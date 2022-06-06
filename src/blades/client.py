import copy
import logging
from collections import defaultdict
from typing import Union, Callable, Tuple

import ray.train as train
import torch
import torch.nn as nn
import torchvision.transforms as transforms
from torch.utils.data import DataLoader


class TorchClient(object):
    _is_byzantine = False
    
    def __init__(
            self,
            client_id=None,
            device: Union[torch.device, str]='cpu',
    ):
        self.id = client_id
        self.device = device
        
        self.running = {}
        self.state = defaultdict(dict)
        self.json_logger = logging.getLogger("stats")
        self.debug_logger = logging.getLogger("debug")
    
    def detach_model(self, lr):
        self.model = copy.deepcopy(self.model)
        self.optimizer = torch.optim.SGD(self.model.parameters(), lr=lr)
        # self.optimizer = torch.optim.SGD(self.model.parameters(), lr=self.optimizer.param_groups[0]['lr'])
    
    def set_id(self, id):
        self.id = id
        
    def get_id(self):
        return self.id
        
    def getattr(self, attr):
        return getattr(self, attr)
    
    def get_is_byzantine(self):
        return self._is_byzantine
    
    def set_model(self, model, opt, lr):
        self.model = copy.deepcopy(model)
        self.optimizer = opt(self.model.parameters(), lr=lr)
    
    def omniscient_callback(self, simulator):
        pass
    
    def set_loss(self, loss_func='crossentropy'):
        if loss_func == 'crossentropy':
            self.loss_func = nn.modules.loss.CrossEntropyLoss()
        else:
            raise NotImplementedError
    
    def set_para(self, model):
        self.model.load_state_dict(model.state_dict())  # .to(self.device)
    
    # def add_metric(self, name: str, callback: Callable[[torch.Tensor, torch.Tensor], float]):
    #     if name in self.metrics or name in ["loss", "length"]:
    #         raise KeyError(f"Metrics ({name}) already added.")
    #
    #     self.metrics[name] = callback
    
    def add_metrics(self, metrics: dict):
        for name in metrics:
            self.add_metric(name, metrics[name])
    
    def __str__(self) -> str:
        return "TorchWorker"
    
    def train_epoch_start(self) -> None:
        # self.running["train_loader_iterator"] = iter(self.data_loader)
        self.model = self.model.to(self.device)
        self.model.train()
    
    def reset_data_loader(self):
        self.running["train_loader_iterator"] = iter(self.data_loader)
    
    def compute_gradient(self) -> Tuple[float, int]:
        try:
            data, target = self.running["train_loader_iterator"].__next__()
        except StopIteration:
            self.reset_data_loader()
            data, target = self.running["train_loader_iterator"].__next__()
        data, target = data.to(self.device), target.to(self.device)
        self.optimizer.zero_grad()
        output = self.model(data)
        loss = self.loss_func(output, target)
        loss.backward()
        self._save_grad()
    
    def evaluate(self, round_number, test_set, batch_size, metrics, use_actor=True):
        cifar10_stats = {
            "mean": (0.4914, 0.4822, 0.4465),
            "std": (0.2023, 0.1994, 0.2010),
        }
        transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(cifar10_stats["mean"], cifar10_stats["std"]),
        ])
        dataloader = DataLoader(dataset=test_set, batch_size=batch_size)
        self.model.eval()
        r = {
            "_meta": {"type": "client_validation"},
            "E": round_number,
            "Length": 0,
            "Loss": 0,
        }
        for name in metrics:
            r[name] = 0
        
        with torch.no_grad():
            for _, (data, target) in enumerate(dataloader):
                data, target = data.to(self.device), target.to(self.device)
                output = self.model.to(self.device)(data)
                r["Loss"] += self.loss_func(output, target).item() * len(target)
                r["Length"] += len(target)
                
                for name, metric in metrics.items():
                    r[name] += metric(output, target) * len(target)
        
        for name in metrics:
            r[name] /= r["Length"]
        r["Loss"] /= r["Length"]
        
        self.json_logger.info(r)
        self.debug_logger.info(
            f"\n=> Eval Loss={r['Loss']:.4f} "
            + " ".join(name + "=" + "{:>8.4f}".format(r[name]) for name in metrics)
            + "\n"
        )
        return r
    
    def local_training(self, num_rounds, use_actor, data_batches) -> Tuple[float, int]:
        self._save_para()
        if use_actor:
            model = self.model
        else:
            model = train.torch.prepare_model(self.model)
        
        for data, target in data_batches:
            data, target = data.to(self.device), target.to(self.device)
            self.optimizer.zero_grad()
            
            output = model(data)
            loss = self.loss_func(output, target)
            loss.backward()
            self.apply_gradient()
        
        self.model = model
        update = (self._get_para(current=True) - self._get_para(current=False))
        self._save_update(update)
    
    def get_gradient(self) -> torch.Tensor:
        return self._get_saved_grad()
    
    def get_update(self) -> torch.Tensor:
        return torch.nan_to_num(self._get_saved_update())
    
    def apply_gradient(self) -> None:
        self.optimizer.step()
    
    def set_gradient(self, gradient: torch.Tensor) -> None:
        beg = 0
        for p in self.model.parameters():
            end = beg + len(p.grad.view(-1))
            x = gradient[beg:end].reshape_as(p.grad.data)
            p.grad.data = x.clone().detach()
            beg = end
    
    def _save_grad(self) -> None:
        for group in self.optimizer.param_groups:
            for p in group["params"]:
                if p.grad is None:
                    continue
                param_state = self.state[p]
                param_state["saved_grad"] = torch.clone(p.grad).detach()
    
    def _save_update(self, update: torch.Tensor) -> None:
        self.state['saved_update'] = update.detach()
    
    def _get_saved_update(self):
        return self.state['saved_update']
    
    def _save_para(self) -> None:
        for group in self.optimizer.param_groups:
            for p in group["params"]:
                if not p.requires_grad:
                    continue
                param_state = self.state[p]
                param_state["saved_para"] = torch.clone(p.data).detach()
    
    def _get_para(self, current=True) -> None:
        layer_parameters = []
        
        for group in self.optimizer.param_groups:
            for p in group["params"]:
                if p.grad is None:
                    continue
                if current:
                    layer_parameters.append(p.data.view(-1))
                else:
                    param_state = self.state[p]
                    layer_parameters.append(param_state["saved_para"].data.view(-1))
        return torch.cat(layer_parameters).to('cpu')
    
    def _get_saved_grad(self) -> torch.Tensor:
        layer_gradients = []
        for group in self.optimizer.param_groups:
            for p in group["params"]:
                param_state = self.state[p]
                layer_gradients.append(param_state["saved_grad"].data.view(-1))
        return torch.cat(layer_gradients)


class ClientWithMomentum(TorchClient):
    """
    Note that we use `WorkerWithMomentum` instead of using multiple `torch.optim.Optimizer`
    because we need to explicitly update the `momentum_buffer`.
    """
    
    def __init__(self, momentum, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.momentum = momentum
    
    def _save_grad(self) -> None:
        for group in self.optimizer.param_groups:
            for p in group["params"]:
                if p.grad is None:
                    continue
                
                param_state = self.state[p]
                if "momentum_buffer" not in param_state:
                    param_state["momentum_buffer"] = torch.clone(p.grad).detach()
                else:
                    param_state["momentum_buffer"].mul_(self.momentum).add_(p.grad.mul_(1 - self.momentum))
    
    def _get_saved_grad(self) -> torch.Tensor:
        layer_gradients = []
        for group in self.optimizer.param_groups:
            for p in group["params"]:
                param_state = self.state[p]
                layer_gradients.append(param_state["momentum_buffer"].data.view(-1))
        return torch.cat(layer_gradients)


class ByzantineClient(TorchClient):
    _is_byzantine = True
    def __int__(self, *args, **kwargs):
        super(ByzantineClient).__init__(*args, **kwargs)
