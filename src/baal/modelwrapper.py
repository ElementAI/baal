import sys
from collections.abc import Sequence
from copy import deepcopy
from typing import Callable, Optional

import numpy as np
import structlog
import torch
from torch.autograd import Variable
from torch.optim import Optimizer
from torch.utils.data import DataLoader, Dataset
from torch.utils.data.dataloader import default_collate
from tqdm import tqdm

from baal.utils.iterutils import map_on_tensor
from baal.utils.metrics import Loss

log = structlog.get_logger("ModelWrapper")


class ModelWrapper:
    """
    Wrapper created to ease the training/testing/loading.

    Args:
        model (nn.Module): The model to optimize.
        criterion (Callable): a loss function.
    """

    def __init__(self, model, criterion):
        self.model = model
        self.criterion = criterion
        self.metrics = dict()
        self.add_metric('loss', lambda: Loss())

    def add_metric(self, name: str, initializer: Callable):
        """
        Add a baal.utils.metric.Metric to the Model.

        Args:
            name (str): name of the metric.
            initializer (Callable): lambda to initialize a new instance of a
                                    baal.utils.metrics.Metric object.
        """
        self.metrics['test_' + name] = initializer()
        self.metrics['train_' + name] = initializer()

    def _reset_metrics(self, filter=''):
        """
        Reset all Metrics according to a filter.

        Args:
            filter (str): Only keep the metric if `filter` in the name.
        """
        for k, v in self.metrics.items():
            if filter in k:
                v.reset()

    def _update_metrics(self, out, target, loss, filter=''):
        """
        Update all metrics.

        Args:
            out (Tensor): Prediction
            target (Tensor): Ground truth
            loss (Tensor): Loss from the criterion.
            filter (str): Only update metrics according to this filter.
        """
        for k, v in self.metrics.items():
            if filter in k:
                if 'loss' in k:
                    v.update(loss)
                else:
                    v.update(out, target)

    def train_on_dataset(self, dataset, optimizer, batch_size, epoch, use_cuda, workers=4,
                         collate_fn: Optional[Callable] = None):
        """
        Train for `epoch` epochs on a Dataset `dataset.

        Args:
            dataset (Dataset): Pytorch Dataset to be trained on.
            optimizer (optim.Optimizer): Optimizer to use.
            batch_size (int): The batch size used in the DataLoader.
            epoch (int): Number of epoch to train for.
            use_cuda (bool): Use cuda or not.
            workers (int): Number of workers for the multiprocessing.
            collate_fn (Optional[Callable]): The collate function to use.

        Returns:
            The training history.
        """
        self.train()
        history = []
        log.info("Starting training", epoch=epoch, dataset=len(dataset))
        collate_fn = collate_fn or default_collate
        for _ in range(epoch):
            self._reset_metrics('train')
            for data, target in DataLoader(dataset, batch_size, True, num_workers=workers,
                                           collate_fn=collate_fn):
                _ = self.train_on_batch(data, target, optimizer, use_cuda)
            history.append(self.metrics['train_loss'].value)

        optimizer.zero_grad()  # Assert that the gradient is flushed.
        log.info("Training complete", train_loss=self.metrics['train_loss'].value)
        return history

    def test_on_dataset(
        self,
        dataset: Dataset,
        batch_size: int,
        use_cuda: bool,
        workers: int = 4,
        collate_fn: Optional[Callable] = None,
        average_predictions: int = 1,
    ):
        """
        Test the model on a Dataset `dataset`.

        Args:
            dataset (Dataset): Dataset to evaluate on.
            batch_size (int): Batch size used for evaluation.
            use_cuda (bool): Use Cuda or not.
            workers (int): Number of workers to use.
            collate_fn (Optional[Callable]): The collate function to use.
            average_predictions (int): The number of predictions to average to
                compute the test loss.

        Returns:
            Average loss value over the dataset.
        """
        self.eval()
        log.info("Starting evaluating", dataset=len(dataset))
        self._reset_metrics('test')

        collate_fn = collate_fn or default_collate
        for data, target in DataLoader(dataset, batch_size, False, num_workers=workers,
                                       collate_fn=collate_fn):
            _ = self.test_on_batch(
                data, target, cuda=use_cuda, average_predictions=average_predictions
            )

        log.info("Evaluation complete", test_loss=self.metrics['test_loss'].value)
        return self.metrics['test_loss'].value

    def train_and_test_on_datasets(self, train_dataset: Dataset, test_dataset: Dataset,
                                   optimizer: Optimizer, batch_size: int, epoch: int,
                                   use_cuda: bool,
                                   workers: int = 4,
                                   collate_fn: Optional[Callable] = None,
                                   return_best_weights=False):
        """
        Train and test the model on both Dataset `train_dataset`, `test_dataset`.

        Args:
            train_dataset (Dataset): Dataset to train on.
            test_dataset (Dataset): Dataset to evaluate on.
            optimizer (Optimizer): Optimizer to use during training.
            batch_size (int): Batch size used.
            epoch (int): number of epoch to train on.
            use_cuda (bool): Use Cuda or not.
            workers (int): Number of workers to use.
            collate_fn (Optional[Callable]): The collate function to use.
            return_best_weights (bool): If True, will keep the best weights and return them.

        Returns:
            History and best weights if required.
        """
        best_weight = None
        best_loss = 1e10
        hist = []
        for epoch in range(epoch):
            _ = self.train_on_dataset(train_dataset, optimizer, batch_size, 1,
                                      use_cuda, workers, collate_fn)
            te_loss = self.test_on_dataset(test_dataset, batch_size, use_cuda, workers, collate_fn)
            hist.append({k: v.value for k, v in self.metrics.items()})
            if return_best_weights and te_loss < best_loss:
                best_loss = te_loss
                best_weight = deepcopy(self.state_dict())

        if return_best_weights:
            return hist, best_weight
        else:
            return hist

    def predict_on_dataset_generator(self, dataset: Dataset, batch_size: int, iterations: int,
                                     use_cuda: bool, workers: int = 4,
                                     collate_fn: Optional[Callable] = None, half=False):
        """
        Use the model to predict on a dataset `iterations` time.

        Args:
            dataset (Dataset): Dataset to predict on.
            batch_size (int):  Batch size to use during prediction.
            iterations (int): Number of iterations per sample.
            use_cuda (bool): Use CUDA or not.
            workers (int): Number of workers to use.
            collate_fn (Optional[Callable]): The collate function to use.
            half (bool): if True use half precision.

        Notes:
            The "batch" is made of `batch_size` * `iterations` samples.

        Returns:
            Generators [batch_size, n_classes, ..., n_iterations]
        """
        self.eval()
        if len(dataset) == 0:
            return None

        log.info("Start Predict", dataset=len(dataset))
        collate_fn = collate_fn or default_collate
        loader = DataLoader(dataset,
                            batch_size,
                            False, num_workers=workers,
                            collate_fn=collate_fn)
        for idx, (data, _) in enumerate(tqdm(loader, total=len(loader), file=sys.stdout)):

            pred = self.predict_on_batch(data, iterations, use_cuda)
            pred = map_on_tensor(lambda x: x.detach(), pred)
            if half:
                pred = map_on_tensor(lambda x: x.half(), pred)
            yield map_on_tensor(lambda x: x.cpu().numpy(), pred)

    def predict_on_dataset(self, dataset: Dataset, batch_size: int, iterations: int, use_cuda: bool,
                           workers: int = 4, collate_fn: Optional[Callable] = None, half=False):
        """
        Use the model to predict on a dataset `iterations` time.

        Args:
            dataset (Dataset): Dataset to predict on.
            batch_size (int):  Batch size to use during prediction.
            iterations (int): Number of iterations per sample.
            use_cuda (bool): Use CUDA or not.
            workers (int): Number of workers to use.
            collate_fn (Optional[Callable]): The collate function to use.
            half (bool): if True use half precision

        Notes:
            The "batch" is made of `batch_size` * `iterations` samples.

        Returns:
            Array [n_samples, n_outputs, ..., n_iterations].
        """
        preds = list(self.predict_on_dataset_generator(dataset=dataset, batch_size=batch_size,
                                                       iterations=iterations, use_cuda=use_cuda,
                                                       workers=workers, collate_fn=collate_fn,
                                                       half=half))

        if len(preds) > 0 and not isinstance(preds[0], Sequence):
            # Is an Array or a Tensor
            return np.vstack(preds)
        return [np.vstack(pr) for pr in zip(*preds)]

    def train_on_batch(self, data, target, optimizer, cuda=False):
        """
        Train the current model on a batch using `optimizer`.

        Args:
            data (Tensor): the model input
            target (Tensor): the ground truth
            optimizer (optim.Optimizer): an optimizer
            cuda (bool): use cuda or not

        Returns:
            Tensor, the loss computed from the criterion.
        """
        data = Variable(data)
        if cuda:
            data, target = data.cuda(), target.cuda()
        optimizer.zero_grad()
        output = self.model(data)
        loss = self.criterion(output, target)
        loss.backward()
        optimizer.step()
        self._update_metrics(output, target, loss, filter='train')
        return loss

    def test_on_batch(
        self,
        data: torch.Tensor,
        target: torch.Tensor,
        cuda: bool = False,
        average_predictions: int = 1,
    ):
        """
        Test the current model on a batch.

        Args:
            data (Tensor): the model input
            target (Tensor): the ground truth
            cuda (bool): use cuda or not
            average_predictions (int): The number of predictions to average to
                compute the test loss.

        Returns:
            Tensor, the loss computed from the criterion.
        """
        with torch.no_grad():
            if cuda:
                data, target = data.cuda(), target.cuda()
            if average_predictions == 1:
                preds = self.model(data)
                loss = self.criterion(preds, target)
            elif average_predictions > 1:
                preds = map_on_tensor(lambda p: p.mean(-1),
                                      self.predict_on_batch(data,
                                                            iterations=average_predictions,
                                                            cuda=cuda))
                loss = self.criterion(preds, target)
            self._update_metrics(preds, target, loss, 'test')
            return loss

    def predict_on_batch(self, data, iterations=1, cuda=False):
        """
        Get the model's prediction on a batch.

        Args:
            data (Tensor): the model input
            iterations (int): number of prediction to perform.
            cuda (bool): use cuda or not

        Returns:
            Tensor, the loss computed from the criterion.
                    shape = {batch_size, nclass, n_iteration}
        """
        with torch.no_grad():
            if cuda:
                data = data.cuda()
            input_shape = data.size()
            batch_size = input_shape[0]
            data = torch.stack([data] * iterations)
            data = data.view(batch_size * iterations, *input_shape[1:])
            out = self.model(data)
            out = map_on_tensor(lambda o: o.view([iterations, batch_size, *o.size()[1:]]), out)
            out = map_on_tensor(lambda o: o.permute(1, 2, *range(3, o.ndimension()), 0), out)
            return out

    def get_params(self):
        """
        Return the parameters to optimize.

        Returns:
            Config for parameters.
        """
        return self.model.parameters()

    def state_dict(self):
        """Get the state dict(s)"""
        return self.model.state_dict()

    def load_state_dict(self, state_dict, strict=True):
        """Load the model with `state_dict`"""
        self.model.load_state_dict(state_dict, strict=strict)

    def train(self):
        """Set the model in `train` mode."""
        self.model.train()

    def eval(self):
        """Set the model in `eval mode`."""
        self.model.eval()

    def reset_fcs(self):
        """Reset all torch.nn.Linear layers."""
        def reset(m):
            if isinstance(m, torch.nn.Linear):
                m.reset_parameters()

        self.model.apply(reset)

    def reset_all(self):
        """Reset all *resetable* layers."""
        def reset(m):
            for m in self.model.modules():
                getattr(m, 'reset_parameters', lambda: None)()

        self.model.apply(reset)
