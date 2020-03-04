# -*- coding: utf-8 -*-
import numpy as np
import tensorflow as tf
from tensorflow.keras.datasets import cifar10, cifar100
import time
import sys
sys.path.append('../')

from trainers.generic import GenericExtractor
from augment import augment_function

BIG_NUMBER = 1000.

def _build_simclr_dataset(train_data, imshape=(32,32), batch_size=256, 
                      num_parallel_calls=None, norm=255,
                      num_channels=3, augment=True,
                      single_channel=False):
    """
    
    """
    assert augment, "don't you need to augment your data?"
    _aug = augment_function(imshape, augment)

    ds = tf.data.Dataset.from_tensor_slices(train_data)
    
    @tf.function
    def _augment_and_stack(x):
        y = tf.constant(np.array([1,-1]).astype(np.int32))
        return tf.stack([_aug(x),_aug(x)]), y

    ds = ds.map(_augment_and_stack, num_parallel_calls=num_parallel_calls)
    
    ds = ds.unbatch()
    ds = ds.batch(2*batch_size, drop_remainder=True)
    ds = ds.prefetch(1)
    return ds


def _build_embedding_model(fcn, imshape, num_channels, num_hidden, output_dim):
    """
    Create a Keras model that wraps the base encoder and 
    the projection head
    """
    inpt = tf.keras.layers.Input((imshape[0], imshape[1], num_channels))
    net = fcn(inpt)
    net = tf.keras.layers.Flatten()(net)
    net = tf.keras.layers.Dense(num_hidden, activation="relu")(net)
    net = tf.keras.layers.Dense(output_dim)(net)
    embedding_model = tf.keras.Model(inpt, net)
    return embedding_model



def _build_simclr_training_step(embed_model, optimizer, temperature=0.1, replicas=1):
    """
    Generate a tensorflow function to run the training step for SimCLR.
    
    :embed_model: full Keras model including both the convnet and 
        projection head
    :optimizer: Keras optimizer
    :temperature: hyperparameter for scaling cosine similarities
    :replicas:
    """
    @tf.function
    def training_step(x,y):
        eye = tf.linalg.eye(x.shape[0])
        index = tf.range(0, x.shape[0])
        # the labels tell which similarity is the "correct" one- the augmented
        # pair from the same image. so index+y should look like [1,0,3,2,5,4...]
        labels = index+y

        with tf.GradientTape() as tape:
            # run each image through the convnet and
            # projection head
            embeddings = embed_model(x, training=True)
            # normalize the embeddings
            embeds_norm = tf.nn.l2_normalize(embeddings, axis=1)
            # compute the pairwise matrix of cosine similarities
            sim = tf.matmul(embeds_norm, embeds_norm, transpose_b=True)
            # subtract a large number from diagonals to effectively remove
            # them from the sum, and rescale by temperature
            logits = (sim - BIG_NUMBER*eye)/temperature
            
            loss = tf.reduce_mean(
                    tf.nn.sparse_softmax_cross_entropy_with_logits(labels, logits))/replicas

        gradients = tape.gradient(loss, embed_model.trainable_variables)
        optimizer.apply_gradients(zip(gradients,
                                      embed_model.trainable_variables))
        return loss
    return training_step




class SimCLRTrainer(GenericExtractor):
    """
    Class for training a SimCLR model.
    
    Based on "A Simple Framework for Contrastive Learning of Visual
    Representations" by Chen et al.
    """

    def __init__(self, logdir, trainingdata, testdata=None, fcn=None, 
                 augment=True, temperature=1., num_hidden=128,
                 output_dim=64,
                 lr=0.01, lr_decay=10000,
                 imshape=(32,32), num_channels=3,
                 norm=255, batch_size=64, num_parallel_calls=None,
                 single_channel=False, notes="",
                 downstream_labels=None):
        """
        :logdir: (string) path to log directory
        :trainingdata: (list) list of paths to training images
        :testdata: (list) filepaths of a batch of images to use for eval
        :fcn: (keras Model) fully-convolutional network to train as feature extractor
        :augment: (dict) dictionary of augmentation parameters, True for defaults
        :temperature: the Boltmann temperature parameter- rescale the cosine similarities by this factor before computing softmax loss.
        :num_hidden: number of hidden neurons in the network's projection head
        :output_dim: dimension of projection head's output space. Figure 8 in Chen et al's paper shows that their results did not depend strongly on this value.
        :lr: (float) initial learning rate
        :lr_decay: (int) steps for learning rate to decay by half (0 to disable)
        :imshape: (tuple) image dimensions in H,W
        :num_channels: (int) number of image channels
        :norm: (int or float) normalization constant for images (for rescaling to
               unit interval)
        :batch_size: (int) batch size for training
        :num_parallel_calls: (int) number of threads for loader mapping
        :single_channel: if True, expect a single-channel input image and 
                stack it num_channels times.
        :notes: (string) any notes on the experiment that you want saved in the
                config.yml file
        :downstream_labels: dictionary mapping image file paths to labels
        """
        assert augment is not False, "this method needs an augmentation scheme"
        self.logdir = logdir
        self.trainingdata = trainingdata
        
        (train_data, train_labels), (test_data, test_labels) = cifar10.load_data()
        self.train_data = (train_data / 255.0).astype(np.float32)
        self.test_data = (test_data / 255.0).astype(np.float32)

        self._downstream_train_labels = train_labels.ravel()
        self._downstream_test_labels = test_labels.ravel()

        self._file_writer = tf.summary.create_file_writer(logdir, flush_millis=10000)
        self._file_writer.set_as_default()
        
        # if no FCN is passed- build one
        if fcn == 'ResNet50':
            fcn = tf.keras.applications.ResNet50V2(weights=None, include_top=False)
        self.fcn = fcn
        # Create a Keras model that wraps the base encoder and 
        # the projection head
        embed_model = _build_embedding_model(fcn, imshape, num_channels,
                                             num_hidden, output_dim)
        
        self._models = {"fcn":fcn, 
                        "full":embed_model}
        
        # build training dataset
        self._ds = _build_simclr_dataset(self.train_data, 
                                        imshape=imshape, batch_size=batch_size,
                                        num_parallel_calls=num_parallel_calls, 
                                        norm=norm, num_channels=num_channels, 
                                        augment=augment,
                                        single_channel=single_channel)
        
        # create optimizer
        if lr_decay > 0:
            learnrate = tf.keras.optimizers.schedules.ExponentialDecay(lr, 
                                            decay_steps=lr_decay, decay_rate=0.5,
                                            staircase=False)
        else:
            learnrate = lr
        self._optimizer = tf.keras.optimizers.Adam(learnrate)
        
        
        # build training step
        self._training_step = _build_simclr_training_step(
                embed_model, 
                self._optimizer, 
                temperature)
        
        self._test = False
        self._test_labels = None
        self._old_test_labels = None
       
        self.start_time = time.time()
        self.step = 0
        
        # parse and write out config YAML
        self._parse_configs(augment=augment, temperature=temperature,
                            num_hidden=num_hidden, output_dim=output_dim,
                            lr=lr, lr_decay=lr_decay, 
                            imshape=imshape, num_channels=num_channels,
                            norm=norm, batch_size=batch_size,
                            num_parallel_calls=num_parallel_calls,
                            single_channel=single_channel, notes=notes)

    def _run_training_epoch(self, **kwargs):
        """
        
        """
        for x, y in self._ds:
            loss = self._training_step(x,y)
            if self.step % 100 == 0:
                print("Step: [%2d] time: %4.2f, train_loss: %.4f" % (self.step, time.time() - self.start_time, loss))
            self._record_scalars(loss=loss)
            self.step += 1
            
 
    def evaluate(self):
         # choose the hyperparameters to record
         if not hasattr(self, "_hparams_config"):
             from tensorboard.plugins.hparams import api as hp
             hparams = {
                 hp.HParam("temperature", hp.RealInterval(0., 10000.)):self.config["temperature"],
                 hp.HParam("num_hidden", hp.IntInterval(1, 1000000)):self.config["num_hidden"],
                 hp.HParam("output_dim", hp.IntInterval(1, 1000000)):self.config["output_dim"]
                 }
         else:
             hparams=None
         self._linear_classification_test(hparams)
        
