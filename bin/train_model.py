# -*- coding: utf-8 -*-
"""
Training script for the model with cross entropy as the loss function.
"""
import sys
import os
import json
import copy
import argparse
import chainer
import chainer.functions as F
import chainer.links as L
from chainer import training
from chainer import reporter
from chainer import cuda
from chainer.training import extensions
from datetime import datetime

from NER import Resource
from NER import NERTagger, BiNERTagger, BiCharNERTagger
from NER import DataProcessor
import numpy as xp
import numpy as np


class Classifier(chainer.Chain):
    compute_accuracy = True

    def __init__(self, predictor, lossfun=F.softmax_cross_entropy,
                 accfun=F.accuracy):
        super(Classifier, self).__init__(predictor=predictor)
        self.lossfun = lossfun
        self.accfun = accfun
        self.y = None
        self.loss = None
        self.accuracy = None

    def __call__(self, *args, train=True):
        assert len(args) >= 2
        x = args[:-1]
        t = args[-1]
        self.y = None
        self.loss = None
        self.accuracy = None
        self.y = self.predictor(*x, train)
        for yi, ti, in zip(self.y, t):
            if self.loss is not None:
                self.loss += self.lossfun(yi, ti)
            else:
                self.loss = self.lossfun(yi, ti)
        reporter.report({'loss': self.loss}, self)

        count = 0
        if self.compute_accuracy:
            for yi, ti in zip(self.y, t):
                if self.accuracy is not None:
                    self.accuracy += self.accfun(yi, ti) * len(ti)
                    count += len(ti)
                else:
                    self.accuracy = self.accfun(yi, ti) * len(ti)
                    count += len(ti)
            reporter.report({'accuracy': self.accuracy / count}, self)
        return self.loss, self.accuracy, count


class LSTMUpdater(training.StandardUpdater):

    def __init__(self, iterator, optimizer, device, unit, singleton):
        super(LSTMUpdater, self).__init__(
            iterator=iterator, optimizer=optimizer)
        if device >= 0:
            self.xp = cuda.cupy
        else:
            self.xp = xp
        self.unit = unit
        self.singleton = singleton
        self.id2singleton = {v: k for k, v in singleton.items()}

    def update_core(self):
        batch = self._iterators['main'].next()
        optimizer = self._optimizers['main']
        xs_with_unk = [self.replace_singleton(x[0]) for x in batch]

        xs = [self.xp.array(x, dtype=self.xp.int32) for x in xs_with_unk]
        ts = [self.xp.array(x[2], dtype=self.xp.int32) for x in batch]

        optimizer.target.cleargrads()
        hx = chainer.Variable(
            self.xp.zeros((1, len(xs), self.unit), dtype=self.xp.float32))
        cx = chainer.Variable(
            self.xp.zeros((1, len(xs), self.unit), dtype=self.xp.float32))
        loss, accuracy, count = optimizer.target(
            xs, hx, cx, ts, train=True)
        loss.backward()
        optimizer.update()

    def replace_singleton(self, x):
        x_array = np.array(x)

        is_singleton = np.array([True if idx in self.id2singleton else False for idx in x], dtype=np.bool)
        bool_mask = np.random.randint(0, 2, size=is_singleton[is_singleton].shape).astype(np.bool)
        is_singleton[is_singleton] = bool_mask

        r = np.zeros(x_array.shape)
        x_array[is_singleton] = r[is_singleton]
        return x_array


class CharLSTMUpdater(training.StandardUpdater):

    def __init__(self, iterator, optimizer, device, unit, singleton):
        super(CharLSTMUpdater, self).__init__(
            iterator=iterator, optimizer=optimizer)
        if device >= 0:
            self.xp = cuda.cupy
        else:
            self.xp = xp
        self.unit = unit
        self.singleton = singleton
        self.id2singleton = {v: k for k, v in singleton.items()}

    def update_core(self):
        batch = self._iterators['main'].next()
        optimizer = self._optimizers['main']

        xs_with_unk = [self.replace_singleton(x[0]) for x in batch]
        xs = [self.xp.array(x, dtype=self.xp.int32) for x in xs_with_unk]
        ts = [self.xp.array(x[2], dtype=self.xp.int32) for x in batch]

        xxs = [[self.xp.array(x, dtype=self.xp.int32)
                for x in sample[1]] for sample in batch]

        optimizer.target.cleargrads()
        hx = chainer.Variable(
            self.xp.zeros((1, len(xs), self.unit + 50), dtype=self.xp.float32))
        cx = chainer.Variable(
            self.xp.zeros((1, len(xs), self.unit + 50), dtype=self.xp.float32))
        loss, accuracy, count = optimizer.target(
            xs, hx, cx, xxs, ts, train=True)
        loss.backward()
        optimizer.update()

    def replace_singleton(self, x):
        x_array = np.array(x)

        is_singleton = np.array([True if idx in self.id2singleton else False for idx in x], dtype=np.bool)
        bool_mask = np.random.randint(0, 2, size=is_singleton[is_singleton].shape).astype(np.bool)
        is_singleton[is_singleton] = bool_mask

        r = np.zeros(x_array.shape)
        x_array[is_singleton] = r[is_singleton]
        return x_array


class LSTMEvaluator(extensions.Evaluator):

    def __init__(self, iterator, target, device, unit):
        super(LSTMEvaluator, self).__init__(
            iterator=iterator, target=target, device=device)
        if device >= 0:
            self.xp = cuda.cupy
        else:
            self.xp = xp
        self.unit = unit

    def evaluate(self):
        iterator = self._iterators['main']
        target = self._targets['main']
        it = copy.copy(iterator)  # これがないと1回しかEvaluationが走らない
        summary = reporter.DictSummary()
        for batch in it:
            observation = {}
            with reporter.report_scope(observation):
                xs = [self.xp.array(x[0], dtype=self.xp.int32) for x in batch]
                ts = [self.xp.array(x[2], dtype=self.xp.int32) for x in batch]
                hx = chainer.Variable(
                    self.xp.zeros((1, len(xs), self.unit), dtype=self.xp.float32))
                cx = chainer.Variable(
                    self.xp.zeros((1, len(xs), self.unit), dtype=self.xp.float32))

                loss = target(xs, hx, cx, ts, train=False)

            summary.add(observation)
        return summary.compute_mean()


class CharLSTMEvaluator(extensions.Evaluator):

    def __init__(self, iterator, target, device, unit):
        super(CharLSTMEvaluator, self).__init__(
            iterator=iterator, target=target, device=device)
        if device >= 0:
            self.xp = cuda.cupy
        else:
            self.xp = xp
        self.unit = unit

    def evaluate(self):
        iterator = self._iterators['main']
        target = self._targets['main']
        it = copy.copy(iterator)  # これがないと1回しかEvaluationが走らない
        summary = reporter.DictSummary()
        for batch in it:
            observation = {}
            with reporter.report_scope(observation):
                xs = [self.xp.array(x[0], dtype=self.xp.int32) for x in batch]
                ts = [self.xp.array(x[2], dtype=self.xp.int32) for x in batch]
                hx = chainer.Variable(
                    self.xp.zeros((1, len(xs), self.unit + 50), dtype=self.xp.float32))
                cx = chainer.Variable(
                    self.xp.zeros((1, len(xs), self.unit + 50), dtype=self.xp.float32))
                xxs = [[self.xp.array(x, dtype=self.xp.int32)
                        for x in sample[1]] for sample in batch]

                loss = target(xs, hx, cx, xxs, ts, train=False)

            summary.add(observation)
        return summary.compute_mean()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batchsize', '-b', type=int, default=20,
                        help='Number of examples in each mini-batch')
    parser.add_argument('--epoch', '-e', type=int, default=6,
                        help='Number of sweeps over the dataset to train')
    parser.add_argument('--gpu', '-g', type=int, default=-1,
                        help='GPU ID (negative value indicates CPU)')
    parser.add_argument('--out', '-o', default='result',
                        help='Directory to output the result')
    parser.add_argument('--resume', '-r', default='',
                        help='Resume the training from snapshot')
    parser.add_argument('--test', action='store_true',
                        help='Use tiny datasets for quick tests')
    parser.set_defaults(test=False)
    parser.add_argument('--unit', '-u', type=int, default=100,
                        help='Number of LSTM units in each layer')
    parser.add_argument('--glove', type=str, default="",
                        help='path to glove vector')
    parser.add_argument('--dropout', action='store_true',
                        help='use dropout?')
    parser.set_defaults(dropout=False)
    parser.add_argument('--model-type', dest='model_type', type=str, required=True,
                        help='bilstm / lstm / char-bi-lstm')
    parser.add_argument('--final-layer', default='withoutCRF',
                        type=str, help='loss function is the cross entropy')
    args = parser.parse_args()

    # save configurations to file
    start_time = datetime.now().strftime('%Y%m%d_%H_%M_%S')
    dest = "../result/" + start_time
    os.makedirs(dest)
    with open(os.path.join(dest, "settings.json"), "w") as fo:
        fo.write(json.dumps(vars(args),  sort_keys=True, indent=4))

    # 学習/validation データの準備
    data_processor = DataProcessor(
        data_path="../work/", use_gpu=args.gpu, test=args.test)
    data_processor.prepare()
    train = data_processor.train_data
    dev = data_processor.dev_data

    train_iter = chainer.iterators.SerialIterator(
        train, batch_size=args.batchsize)
    dev_iter = chainer.iterators.SerialIterator(
        dev, batch_size=args.batchsize, repeat=False)

    # モデルの準備
    optimizer = chainer.optimizers.Adam()
    if args.model_type == "bilstm":
        sys.stderr.write("Using Bidirectional LSTM\n")
        model = Classifier(BiNERTagger(
            n_vocab=len(data_processor.vocab),
            embed_dim=args.unit,
            hidden_dim=args.unit,
            n_tag=len(data_processor.tag),
            dropout=args.dropout
        ))
        optimizer.setup(model)
        optimizer.add_hook(chainer.optimizer.GradientClipping(5))
        updater = LSTMUpdater(train_iter, optimizer,
                              device=args.gpu, unit=args.unit, singleton=data_processor.singleton)
        trainer = training.Trainer(updater, (args.epoch, 'epoch'),
                                   out="../result/" + start_time)
        trainer.extend(LSTMEvaluator(dev_iter, optimizer.target,
                                     device=args.gpu, unit=args.unit))

    elif args.model_type == "lstm":
        sys.stderr.write("Using Normal LSTM\n")
        model = Classifier(NERTagger(
            n_vocab=len(data_processor.vocab),
            embed_dim=args.unit,
            hidden_dim=args.unit,
            n_tag=len(data_processor.tag),
            dropout=args.dropout
        ))
        optimizer.setup(model)
        optimizer.add_hook(chainer.optimizer.GradientClipping(5))
        updater = LSTMUpdater(train_iter, optimizer,
                              device=args.gpu, unit=args.unit, singleton=data_processor.singleton)
        trainer = training.Trainer(updater, (args.epoch, 'epoch'),
                                   out="../result/" + start_time)
        trainer.extend(LSTMEvaluator(dev_iter, optimizer.target,
                                     device=args.gpu, unit=args.unit))

    elif args.model_type == "charlstm":
        sys.stderr.write("Using Bidirectional LSTM with character encoding\n")
        model = Classifier(BiCharNERTagger(
            n_vocab=len(data_processor.vocab),
            n_char=len(data_processor.char),
            embed_dim=args.unit,
            hidden_dim=args.unit,
            n_tag=len(data_processor.tag),
            dropout=args.dropout
        ))
        optimizer.setup(model)
        updater = CharLSTMUpdater(train_iter, optimizer,
                                  device=args.gpu, unit=args.unit, singleton=data_processor.singleton)
        trainer = training.Trainer(updater, (args.epoch, 'epoch'),
                                   out="../result/" + start_time)
        trainer.extend(CharLSTMEvaluator(dev_iter, optimizer.target,
                                         device=args.gpu, unit=args.unit))

    # Send model to GPU (negative value indicates CPU)
    if args.gpu >= 0:
        # Specify GPU ID from command line
        chainer.cuda.get_device(args.gpu).use()
        model.to_gpu()

    # load GloVe vector
    if args.glove:
        sys.stderr.write("loading GloVe...")
        model.predictor.load_glove(args.glove, data_processor.vocab)
        sys.stderr.write("done.\n")

    trainer.extend(extensions.snapshot_object(
        model, 'model_iter_{.updater.iteration}', trigger=(5, 'epoch')))
    trainer.extend(extensions.ProgressBar(update_interval=10))
    trainer.extend(extensions.LogReport())
    trainer.extend(extensions.PrintReport(
        ['epoch', 'main/loss', 'validation/main/loss',
         'main/accuracy', 'validation/main/accuracy', 'elapsed_time']))
    trainer.run()

if __name__ == "__main__":
    main()
