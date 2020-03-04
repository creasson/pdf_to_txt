"""
generic.py

"""
import os
import yaml
import numpy as np

import tensorflow as tf
from tqdm import tqdm
from sklearn.linear_model import SGDClassifier, LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score, confusion_matrix
from tensorboard.plugins.hparams import api as hp

import sys
sys.path.append('../')
from util import dataset


INPUT_PARAMS = ["imshape", "num_channels", "norm", "batch_size",
                "shuffle", "num_parallel_calls", "sobel", "single_channel"]



def linear_classification_test(fcn, train_data,train_labels, 
        test_data,test_labels, **input_config):
    """
    Train a linear classifier on a fully-convolutional network
    and return out-of-sample results.
    
    :fcn: Keras fully-convolutional network
    :train_labels: dictionary mapping image file paths to labels
    :input_config: kwargs for patchwork.loaders.dataset()
    
    Returns
    :acc: float; test accuracy
    :cm: 2D numpy array; confusion matrix
    """
    # get average-pooled training features
    ds, num_steps = dataset(train_data, shuffle=False,
                                            **input_config)
    
    trainvecs = fcn.predict(ds, steps=num_steps).mean(axis=1).mean(axis=1)
    # get test features
    ds, num_steps = dataset(test_data, shuffle=False,
                                            **input_config)
    testvecs = fcn.predict(ds, steps=num_steps).mean(axis=1).mean(axis=1)
    # rescale train and test
    
    scaler = StandardScaler().fit(trainvecs)
    trainvecs = scaler.transform(trainvecs)
    testvecs = scaler.transform(testvecs)
    # train a multinomial classifier
    #logreg = SGDClassifier(loss="log", max_iter=1000, n_jobs=-1, learning_rate="adaptive",
    #                       eta0=1e-2)
    logreg = LogisticRegression(random_state=0, max_iter=2000, solver='lbfgs', C=1.0)
    logreg.fit(trainvecs, train_labels)
    # make predictions on test set
    preds = logreg.predict(testvecs)

    print("Train score:", logreg.score(trainvecs, train_labels))
    print("Test score:", logreg.score(testvecs, test_labels))
    
    # compute metrics and return
    acc = accuracy_score(test_labels, preds)
    cm = confusion_matrix(test_labels, preds)
    return acc, cm





class GenericExtractor(object):
    """
    Place to store common code for different feature extractor methods. Don't 
    actually use this to do anything.
    
    To subclass this, replace:
        __init__
        _build_default_model
        _run_training_epoch
        evaluate
    """
    
    
    def __init__(self, logdir, trainingdata, fcn=None, augment=False, 
                 extractor_param=None, imshape=(256,256), num_channels=3,
                 norm=255, batch_size=64, shuffle=True, num_parallel_calls=None,
                 sobel=False, single_channel=False):
        """
        :logdir: (string) path to log directory
        :trainingdata: (list or tf Dataset) list of paths to training images, or
            dataset to use for training loop
        :fcn: (keras Model) fully-convolutional network to train as feature extractor
        :augment: (dict) dictionary of augmentation parameters, True for defaults or
            False to disable augmentation
        :extractor_param: kwarg for extractor
        :imshape: (tuple) image dimensions in H,W
        :num_channels: (int) number of image channels
        :norm: (int or float) normalization constant for images (for rescaling to
               unit interval)
        :batch_size: (int) batch size for training
        :shuffle: (bool) whether to shuffle training set
        :num_parallel_calls: (int) number of threads for loader mapping
        :sobel: whether to replace the input image with its sobel edges
        :single_channel: if True, expect a single-channel input image and 
            stack it num_channels times.
        """
        self.logdir = logdir
        
        if fcn is None:
            fcn = self._build_default_model()
        self.fcn = fcn
        self._models = {"fcn":fcn}
        
        self._file_writer = tf.summary.create_file_writer(logdir, flush_millis=10000)
        self._file_writer.set_as_default()
        self.step = 0
        
        self._parse_configs(augment=augment, extractor_param=extractor_param,
                            imshape=imshape, num_channels=num_channels,
                            norm=norm, batch_size=batch_size, shuffle=shuffle,
                            num_parallel_calls=num_parallel_calls, sobel=sobel,
                            single_channel=single_channel)
        
        
        
    def _parse_configs(self, **kwargs):
        """
        Organize input parameters and save to a YAML file so you can
        find them later.
        """
        self.config = {}
        self.input_config = {}
        self.augment_config = False
        
        for k in kwargs:
            if k == "augment":
                self.augment_config = kwargs[k]
            elif k in INPUT_PARAMS:
                self.input_config[k] = kwargs[k]
            else:
                self.config[k] = kwargs[k]
                
        config_path = os.path.join(self.logdir, "config.yml")
        config_dict = {"model":self.config, "input":self.input_config, 
                       "augment":self.augment_config}
        yaml.dump(config_dict, open(config_path, "w"), default_flow_style=False)
        
        
    def _build_default_model(self, **kwargs):
        # REPLACE THIS WHEN SUBCLASSING
        return True
    
    def _run_training_epoch(self, **kwargs):
        # REPLACE THIS WHEN SUBCLASSING
        return True
    
    def fit(self, epochs=1, save=True, evaluate=True):
        """
        Train the feature extractor
        
        :epochs: number of epochs to train for
        :save: if True, save after each epoch
        :evaluate: if True, run eval metrics after each epoch
        """
        for e in tqdm(range(epochs)):
            self._run_training_epoch()
            
            if save:
                self.save()
            #if evaluate:
            #    self.evaluate()
    
    def save(self):
        """
        Write model(s) to disk
        
        Note: tried to use SavedModel format for this and got a memory leak;
        think it's related to https://github.com/tensorflow/tensorflow/issues/32234
        
        For now sticking with HDF5
        """
        for m in self._models:
            path = os.path.join(self.logdir, m+".h5")
            self._models[m].save(path, overwrite=True, save_format="h5")
            
    def evaluate(self):
        # REPLACE THIS WHEN SUBCLASSING
        return True
            
    def _record_scalars(self, **scalars):
        for s in scalars:
            tf.summary.scalar(s, scalars[s], step=self.step)
            
    def _record_images(self, **images):
        for i in images:
            tf.summary.image(i, images[i], step=self.step)
            
    def _record_hists(self, **hists):
        for h in hists:
            tf.summary.histogram(h, hists[h], step=self.step)
            
    def _linear_classification_test(self, params=None):
         acc, conf_mat = linear_classification_test(self.fcn,
                                    self.train_data,
                                    self._downstream_train_labels,
                                    self.test_data,
                                    self._downstream_test_labels,
                                    **self.input_config)
         conf_mat = np.expand_dims(np.expand_dims(conf_mat, 0), -1)/conf_mat.max()
         
         self._record_scalars(linear_classification_accuracy=acc)
         self._record_images(linear_classification_confusion_matrix=conf_mat)
         # if the model passed hyperparameters to record for the
         # tensorboard hparams interface:
         if params is not None:
             # first time- set up hparam config
             if not hasattr(self, "_hparams_config"):
                self._hparams_config = hp.hparams_config(
                        hparams=list(params.keys()), 
                        metrics=[hp.Metric("linear_classification_accuracy")])
                
                # record hyperparamters
                hp.hparams(params)
