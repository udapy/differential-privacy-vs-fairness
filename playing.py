from datetime import datetime

import torch
import torchvision
import shutil
import os
import torchvision.transforms as transforms
from collections import defaultdict

from helper import Helper
from image_helper import ImageHelper
from models.simple import Net
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from tqdm import tqdm as tqdm
import visdom
import time
import yaml

from utils.utils import dict_html

vis = visdom.Visdom()
torch.cuda.is_available()
torch.cuda.device_count()

import torchvision.models as models
import logging  
logger = logging.getLogger("logger")
import random

def reseed(seed=5):
    seed = 5
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    random.seed(seed)
    np.random.seed(seed)

class Res(nn.Module):
    def __init__(self):
        reseed()
        super(Res, self).__init__()
        self.res = models.resnet18(pretrained=False)
        # self.fc = nn.Linear(1000, 2)

    def forward(self, x):
        x = self.res(x)
        # x = self.fc(x)
        return x


def plot(x, y, name, win):
    vis.line(Y=np.array([y]), X=np.array([x]),
             win=win,
             name=f'Model_{name}',
             update='append' if vis.win_exists(win) else None,
             opts=dict(showlegend=True, title=win, width=700, height=400)
             )

def compute_norm(model, norm_type=2):
    total_norm = 0
    for p in model.parameters():
        param_norm = p.grad.data.norm(norm_type)
        total_norm += param_norm.item() ** norm_type
    total_norm = total_norm ** (1. / norm_type)
    return total_norm

def test(net, epoch, name, testloader, vis=True, win='Test'):
    net.eval()
    correct = 0
    total = 0
    with torch.no_grad():
        for data in testloader:
            inputs, labels = data
            inputs = inputs.cuda()
            labels = labels.cuda()
            outputs = net(inputs)
            _, predicted = torch.max(outputs.data, 1)
            total += labels.size(0)
            correct += (predicted == labels).sum().item()

    logger.info(f'Name: {name}. Epoch {epoch}. acc: {100 * correct / total}')
    if vis:
        plot(epoch, 100*correct/total, name, win=win)
    return 100 * correct / total


def train_dp(trainloader, model, optimizer, epoch, name):
    norm_type = 2
    model.train()
    running_loss = 0.0
    for i, data in tqdm(enumerate(trainloader, 0), leave=True):
        inputs, labels = data
        inputs = inputs.cuda()
        labels = labels.cuda()
        optimizer.zero_grad()

        outputs = model(inputs)
        loss = criterion(outputs, labels)
        running_loss += torch.mean(loss).item()

        losses = torch.mean(loss.reshape(num_microbatches, -1), dim=1)
        saved_var = dict()
        for tensor_name, tensor in model.named_parameters():
            saved_var[tensor_name] = torch.zeros_like(tensor)

        for j in losses:
            j.backward(retain_graph=True)
            torch.nn.utils.clip_grad_norm_(model.parameters(), S)
            for tensor_name, tensor in model.named_parameters():
                new_grad = tensor.grad
                saved_var[tensor_name].add_(new_grad)
            model.zero_grad()

        for tensor_name, tensor in model.named_parameters():
            saved_var[tensor_name].add_(torch.cuda.FloatTensor(tensor.grad.shape).normal_(0, sigma))
            tensor.grad = saved_var[tensor_name] / num_microbatches
        optimizer.step()

        if i > 0 and i % 20 == 0:
            #             logger.info('[%d, %5d] loss: %.3f' %
            #                   (epoch + 1, i + 1, running_loss / 2000))
            plot(epoch * len(trainloader) + i, running_loss, name, win='Train Loss')
            running_loss = 0.0

def train(trainloader, model, optimizer, epoch, name):

    model.train()
    running_loss = 0.0
    for i, data in tqdm(enumerate(trainloader, 0), leave=True):
        # get the inputs
        inputs, labels = data
        inputs = inputs.cuda()
        labels = labels.cuda()
        # zero the parameter gradients
        optimizer.zero_grad()

        # forward + backward + optimize
        outputs = model(inputs)
        loss = criterion(outputs, labels)

        loss.backward()
        optimizer.step()
        # print statistics
        running_loss += loss.item()
        if i > 0 and i % 20 == 0:
            #             logger.info('[%d, %5d] loss: %.3f' %
            #                   (epoch + 1, i + 1, running_loss / 2000))
            plot(epoch * len(trainloader) + i, running_loss, name, win='Train Loss')
            running_loss = 0.0



if __name__ == '__main__':

    with open('utils/params.yaml') as f:
        params = yaml.load(f)
    helper = ImageHelper(current_time=datetime.now().strftime('%b.%d_%H.%M.%S'), params=params, name='utk')
    batch_size = int(helper.params['batch_size'])
    num_microbatches = int(helper.params['num_microbatches'])
    lr = float(helper.params['lr'])
    momentum = float(helper.params['momentum'])
    decay = float(helper.params['decay'])
    epochs = int(helper.params['epochs'])
    S = float(helper.params['S'])
    z = float(helper.params['z'])
    sigma = z*S
    dp = helper.params['dp']
    mu = helper.params['mu']
    logger.info(f'DP: {dp}')



    logger.info(batch_size)
    logger.info(lr)
    logger.info(momentum)
    helper.load_cifar_data()
    helper.create_loaders()
    helper.sampler_per_class()
    helper.sampler_exponential_class(mu=mu)

    net = Res()
    net.cuda()
    if dp:
        criterion = nn.CrossEntropyLoss(reduction='none')
    else:
        criterion = nn.CrossEntropyLoss()
    optimizer = optim.SGD(net.parameters(), lr=lr, momentum=momentum, weight_decay=decay)
    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer,
                                                     milestones=[0.5 * epochs,
                                                                 0.75 * epochs],
                                                     gamma=0.1)

    win_name = f'DP: {dp}, S: {S}, z: {z}, BS: {batch_size}, Mom: {momentum}, LR: {lr}, DEC:{decay}, ' \
        f'MB: {num_microbatches}, mu: {mu}.'
    name = helper.current_time

    acc = test(net, 0, name, helper.test_loader, vis=True, win=win_name)
    for epoch in range(1,epochs):  # loop over the dataset multiple times
        if dp:
            train_dp(helper.train_loader, net, optimizer, epoch, name)
        else:
            train(helper.train_loader, net, optimizer, epoch, name)
        scheduler.step()
        acc = test(net, epoch, name, helper.test_loader, vis=True, win=win_name)
        acc_list = list()
        for class_no, loader in helper.per_class_loader.items():
            acc_list.append(test(net, epoch, class_no, loader, vis=False, win=win_name))
        plot(epoch, np.var(acc_list), name=name + '_var', win=win_name + '_class_acc')
        plot(epoch, np.max(acc_list), name=name + '_max', win=win_name + '_class_acc')
        plot(epoch, np.min(acc_list), name=name + '_min', win=win_name + '_class_acc')
        helper.save_model(net, epoch, acc)



