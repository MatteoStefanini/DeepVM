import argparse
import numpy as np
from torch.utils.data import Dataset, DataLoader
import json, math, os
import torch
import torch.nn as nn
import torch.optim as optim
from torch.nn import functional as F
from sklearn.metrics import precision_recall_fscore_support
import pandas as pd
from random import shuffle
from tensorboardX import SummaryWriter


columns_set = ['small', 'medium', 'complete']
model_names = ['DeepConv', 'DeepFFT']
parser = argparse.ArgumentParser(description='deepVM training PyTorch')
parser.add_argument('-data', metavar='Folder', default='VMdata/',
                    help='path to dataset folder')
parser.add_argument('--columns', '-c', metavar='Cols_set', default='complete',
                    choices=columns_set, help='columns set: ' + ' | '.join(columns_set) + ' (default: complete)')
parser.add_argument('--window', '-w', default=256, type=int,
                    help='number of timestep for each window')
parser.add_argument('--arch', '-a', metavar='ARCH', default='DeepConv',
                    choices=model_names, help='model architecture: ' + ' | '.join(model_names) + ' (default: DeepConv)')
parser.add_argument('--epochs', '-e', default=110, type=int, metavar='E',
                    help='number of total epochs to run')
parser.add_argument('--batch-size', '-b', default=64, type=int,
                    metavar='B', help='mini-batch size (default: 64)')
parser.add_argument('-lr', '--learning-rate', default=0.00002, type=float,
                    metavar='LR', help='initial learning rate (default: 0.0005)')
parser.add_argument('--weight-decay', '-wd', default=5e-4, type=float,
                    metavar='Wdecay', help='weight decay (default: 1e-4)')
parser.add_argument('--threshold', '-th', default=0, type=int,
                    help='percentage threshold to consider valid data')
parser.add_argument('--freqs', '-fq', default=1., action="store", dest="freqs", type=float,
                    help='percentage of frequences to take into account (only with DeepFFT ARCH)')
parser.add_argument('--workers', '-j',  default=0, type=int, metavar='N',
                    help='number of data loading workers (default: 0)')


class VMDataset(Dataset):
    def __init__(self, data, kind):
        super(VMDataset, self).__init__()
        self.data = data
        self.kind = kind

    def __len__(self):
        return len(self.data)

    def __getitem__(self, i):
        item = self.data[i]
        return torch.from_numpy(np.array(item.drop('label', axis=1), dtype='float32')).requires_grad_(), \
               torch.from_numpy(np.array(item.iloc[0]['label'], dtype='float32'))

    def __str__(self):
        s = "[Kind: {0} Lenght: {1})]".format(self.kind, str(len(self.data)))
        return s


class DeepConv(nn.Module):
    def __init__(self, channels=16, window=128, num_classes=2):
        super(DeepConv, self).__init__()
        self.num_classes = num_classes
        self.channels = channels

        self.layers = list()
        self.num_layers = int(max(math.log2(window) - 1, 2))
        self.stride = 2

        for i in range(0, 2 * self.num_layers, 2):
            i = 1 if i == 0 else i
            if window < 8 and i == 2:
                self.stride = 1
            if i <= 4:
                self.layers.append(nn.Conv1d(i * channels, 2 * i * channels, 3, stride=self.stride, padding=1))
                self.layers.append(nn.BatchNorm1d(2 * i * channels, track_running_stats=False))
                self.layers.append(nn.ReLU(True))
            else:
                self.layers.append(nn.Conv1d(8 * channels, 8 * channels, 3, stride=self.stride, padding=1))
                self.layers.append(nn.BatchNorm1d(8 * channels, track_running_stats=False))
                self.layers.append(nn.ReLU(True))

        self.cnn = nn.Sequential(*self.layers)
        l = 2 if window > 8 else 1
        self.fc = nn.Linear(8 * l * channels, num_classes)

    def forward(self, x):
        x = self.cnn(x).view(x.shape[0], -1)
        x = self.fc(x)

        return x


class DeepFFT(nn.Module):
    def __init__(self, channels=16, window=128, num_classes=2):
        super(DeepFFT, self).__init__()
        self.num_classes = num_classes
        self.channels = channels
        self.num_freq = int(window*args.freqs)

        self.layers = list()
        self.num_layers = int(max(math.log2(window) - 1, 2))
        self.stride = 2

        for i in range(0, 2 * self.num_layers, 2):
            i = 1 if i == 0 else i
            if window < 8 and i == 2:
                self.stride = 1
            if i <= 4:
                self.layers.append(nn.Conv1d(i * channels, 2 * i * channels, 3, stride=self.stride, padding=1))
                self.layers.append(nn.BatchNorm1d(2 * i * channels, track_running_stats=False))
                self.layers.append(nn.ReLU(True))
            else:
                self.layers.append(nn.Conv1d(8 * channels, 8 * channels, 3, stride=self.stride, padding=1))
                self.layers.append(nn.BatchNorm1d(8 * channels, track_running_stats=False))
                self.layers.append(nn.ReLU(True))

        self.cnn = nn.Sequential(*self.layers)
        l = 2 if window > 8 else 1
        self.fc = nn.Linear(8 * l * channels, num_classes)

    def forward(self, x):
        im = torch.zeros_like(x).view(x.shape[0], x.shape[1], x.shape[2], 1)
        x2 = x.view(x.shape[0], x.shape[1], x.shape[2], 1)
        xc = torch.cat([x2, im], dim=3)
        fr = torch.fft(xc, signal_ndim=1)  # tensor with last dimension 2 (real+imag) , 1 is signal dimension

        xr = fr[:, :, :self.num_freq, 0]
        xi = fr[:, :, :self.num_freq, 1]

        x = torch.sqrt(xr ** 2 + xi ** 2)  # magnitude

        x = self.cnn(x).view(x.shape[0], -1)
        x = self.fc(x)

        return x


class ChunkGenerator:

    def __init__(self, df, size, offset, overlap, thr):
        self.valid = False
        self.currData = None
        self.df = df
        self.size = size
        self.overlap = overlap
        self.thr = thr
        self.nxt = max(0, offset - overlap)

    def again(self):
        if self.nxt is None:
            return False
        else:
            return True

    def get_chunk(self):
        if self.nxt + self.size > self.df.shape[0]:
            self.nxt = None
        if not self.again():
            return None
        d = self.df[self.nxt:self.nxt + self.size]
        self.nxt = max(0, self.nxt + self.size - self.overlap)
        # check for idle data
        if d[['CPU%']].mean().values[0] < self.thr:
            self.valid = False
            self.currData = None
        else:
            self.valid = True
            self.currData = d
        return d

    def __str__(self):
        s = "valid: %s, next: %d" % (self.valid, self.nxt if self.nxt is not None else -1)
        return s


def merge_filter(files, interestingColumns):
    for f in files:
        df = pd.read_table("VMdata/"+f, delim_whitespace=True, usecols=interestingColumns)
        try:
            df_tot = df_tot.append(df, ignore_index=True)
        except NameError:
            df_tot = df

    return df_tot


def load_chunks(normalize=False):

    filesWeb = ["WEB1PRO.data", "WEB2PRO.data", "WEB3PRO.data", "WEB4PRO.data"]
    filesSql = ["SQL1PRO.data", "SQL2PRO.data", "SQL3PRO.data", "SQL4PRO.data"]
    channels = 0

    if args.columns == 'small':
        interestingColumns=['CPU%', 'Memory%', 'SysCallRate', 'InPktRate', 'OutPktRate']
        channels = 5
    elif args.columns == 'medium':
        interestingColumns=['CPU%', 'Memory%', 'SysCallRate', 'InPktRate', 'OutPktRate', 'NetworkPktRt', 'AliveProc']
        channels = 7
    elif args.columns == 'complete':
        interestingColumns = ['SysCallRate', 'CPU%', 'IdleCPU%', 'PkFSSp%', 'CacheRdRt', 'Memory%', 'UserMem%', 'PgOutRate',
                          'PageOut', 'Sys+Cache%', 'SysMem%', 'InPktRate', 'OutPktRate', 'NetworkPktRt', 'AliveProc',
                          'ActiveProc']
        channels = 16

    w = args.window  # 256
    if w > 64:
        overlap = 0.75
    else:
        overlap = 0
    thr = args.threshold  # 0

    # merge dataframes
    dfWeb = merge_filter(filesWeb, interestingColumns)
    dfSql = merge_filter(filesSql, interestingColumns)

    if normalize:
        dfWeb = (dfWeb - pd.concat([dfWeb, dfSql]).mean()) / pd.concat([dfWeb, dfSql]).std()
        dfSql = (dfSql - pd.concat([dfWeb, dfSql]).mean()) / pd.concat([dfWeb, dfSql]).std()

    dfWeb.name = "Web"
    dfWeb['label'] = 1
    dfSql.name = "SQL"
    dfSql['label'] = 0

    # Summary print
    for df in [dfSql, dfWeb]:
        print(df.name, df.shape)

    data = dict()
    for df in [dfSql, dfWeb]:
        print("df size: ", df.shape)
        cg = ChunkGenerator(df, w, 0, int(w * overlap), thr)
        df.c = len(interestingColumns)
        df.w = w
        df.overlap = int(w * overlap)

        data[df.name] = list()
        while cg.again():
            d = cg.get_chunk()
            data[df.name].append(d)

    return data, channels


def split(data, train_fraction=0.7, val_fraction=0.2, test_fraction=0.1):
    num_records = []
    num_records.append(len(data["Web"]))
    num_records.append(len(data["SQL"]))

    items_per_class = min(num_records)

    ntrain = int(items_per_class * train_fraction)
    web_train = data["Web"][:ntrain]
    sql_train = data["SQL"][:ntrain]
    train = web_train + sql_train
    print('lenght train data: ', len(train))
    #train = shuffle(train)

    nval = int(items_per_class * val_fraction)
    web_val = data["Web"][ntrain : ntrain + nval]
    sql_val = data["SQL"][ntrain : ntrain + nval]
    val = web_val + sql_val
    print('lenght val data: ', len(val))
    #val = shuffle(val)

    ntest = int(items_per_class * test_fraction)
    web_test = data["Web"][ntrain + nval : ntrain + nval + ntest - 1]
    sql_test = data["SQL"][ntrain + nval : ntrain + nval + ntest - 1]
    test = web_test + sql_test
    print('lenght test data: ', len(test))
    #test = shuffle(test)

    return train, val, test


def training(train, val, test, channels=16):
    writer = SummaryWriter()

    model_name = args.arch
    window = args.window
    batch_s = args.batch_size
    epochs = args.epochs
    lr = args.learning_rate
    wd = args.weight_decay
    freq = args.freqs

    if model_name == "DeepConv":
        model = DeepConv(channels=channels, window=window).to(device)
    elif model_name == "DeepFFT":
        model = DeepFFT(channels=channels, window=window).to(device)  # .double()
    elif model_name == "DeepMix":
        model = DeepMix(channels=channels, window=window).to(device)
    elif model_name == "DeepMix2":
        model = DeepMix2(channels=channels, window=window).to(device)
    else:
        model = DeepConv(channels=channels, window=window).to(device)
    print(model)

    dataset_train = VMDataset(train, 'train')
    dataset_val = VMDataset(val, 'val')
    dataset_test = VMDataset(test, 'test')

    train_dataloader = DataLoader(dataset_train, batch_size=batch_s, num_workers=args.workers)
    val_dataloader = DataLoader(dataset_val, batch_size=batch_s, num_workers=args.workers)

    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=wd)
    #scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=60, gamma=0.5)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.6)
    best_acc = 0
    name_model = args.columns+"_window"+str(window)+"_"+model_name+"_freqs"+str(freq)+"_lr%0.5f_wd%0.5f_epochs"%(lr, wd)+".pt"

    for epoch in range(epochs):
        print("Epoch: %d of %d" % (epoch+1, epochs))
        writer.add_text('Text', 'text logged at step:' + str(epoch), epoch)
        loss_epoch = 0.0; total = 0; correct = 0
        model.train()
        dataloader_iter = iter(train_dataloader)
        for it, (vmdata, labels) in enumerate(dataloader_iter):
            vmdata.transpose_(1, 2)
            vmdata = torch.tensor(vmdata).to(device)#.float()
            labels = torch.tensor(labels).to(device).long()

            output = model(vmdata)
            loss = criterion(output, labels)
            loss_epoch += loss.item()

            _, predicted = torch.max(output.data, 1)
            total += len(labels)
            correct += (predicted == labels.data).sum()

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            if (it*batch_s)%400 == 0:
                print("Processing Training epoch %d (%d/%d) %3.2f%%" % (epoch+1,it*batch_s,len(dataset_train),100*(it*batch_s/len(dataset_train))))

        train_loss = loss_epoch#/(len(dataset) / batch_s)
        train_accuracy = 100 * float(correct.tolist() / total)
        writer.add_scalar('data/loss_train', train_loss, epoch)
        writer.add_scalar('data/accuracy_train', train_accuracy, epoch)
        print("Training epoch %d -- Loss: %6.2f - Accuracy: %3.3f%%" % (epoch+1, train_loss, train_accuracy))

        # EVALUATION
        loss_eval = 0.0; total = 0; correct = 0
        tot_labels = list(); tot_predicted = list()
        model.eval()
        dataloader_val_iter = iter(val_dataloader)
        for it, (vmdata, labels) in enumerate(dataloader_val_iter):
            vmdata.transpose_(1, 2)
            vmdata = torch.tensor(vmdata).to(device)#.float()
            labels = torch.tensor(labels).to(device).long()

            output = model(vmdata)
            loss = criterion(output, labels)
            loss_eval += loss.item()

            _, predicted = torch.max(output.data, 1)
            total += len(labels)
            correct += (predicted == labels.data).sum()
            tot_predicted.append(predicted.cpu().detach().numpy())
            tot_labels.append(labels.cpu().detach().numpy())

            if (it*batch_s)%280 == 0:
                print("Evaluation epoch %d (%d/%d) %3.2f%%" % (epoch+1,it*batch_s,len(dataset_val),100*(it*batch_s/len(dataset_val))))

        tot_predicted = np.concatenate(tot_predicted, axis=0)
        tot_labels = np.concatenate(tot_labels, axis=0)
        m = precision_recall_fscore_support(tot_labels, tot_predicted, average='macro')
        eval_loss = loss_eval#/(len(dataset_test) / batch_s)
        scheduler.step(eval_loss)
        eval_accuracy = 100 * float(correct.tolist() / total)
        writer.add_scalar('data/loss_val', eval_loss, epoch)
        writer.add_scalar('data/accuracy_val', eval_accuracy, epoch)

        if eval_accuracy > best_acc:
            best_acc = eval_accuracy
            torch.save(model, 'models/' + name_model)

        print("Evaluation epoch %d -- Loss: %6.2f - Accuracy: %3.3f%% - F-score: %2.3f" %
              (epoch+1, eval_loss, eval_accuracy, m[2]))

    # TEST final
    r = open("Results_VM/test/"+args.columns+"_window"+str(window)+"_"+model_name+"_freqs"+str(freq)+"_lr%0.5f_wd%0.5f"%(lr, wd)+".csv", "w+")
    r.write("dataset,window,model,perc_freqs,lr,wd,test_loss,precision,recall,fscore,accuracy\n")
    r.write(args.columns+","+str(window)+","+model_name+","+str(freq)+",%1.5f,%1.5f,"%(lr, wd))
    model = torch.load('models/' + name_model)
    test_dataloader = DataLoader(dataset_test, batch_size=batch_s, num_workers=args.workers)
    loss_test = 0.0; total = 0; correct = 0
    tot_labels = list(); tot_predicted = list()
    model.eval()
    dataloader_test_iter = iter(test_dataloader)
    for it, (vmdata, labels) in enumerate(dataloader_test_iter):
        vmdata.transpose_(1, 2)
        vmdata = torch.tensor(vmdata).to(device)
        labels = torch.tensor(labels).to(device).long()

        output = model(vmdata)
        loss = criterion(output, labels)
        loss_test += loss.item()

        _, predicted = torch.max(output.data, 1)
        total += len(labels)
        correct += (predicted == labels.data).sum()
        tot_predicted.append(predicted.cpu().detach().numpy())
        tot_labels.append(labels.cpu().detach().numpy())

    tot_predicted = np.concatenate(tot_predicted, axis=0)
    tot_labels = np.concatenate(tot_labels, axis=0)
    m = precision_recall_fscore_support(tot_labels, tot_predicted, average='macro')
    test_accuracy = 100 * float(correct.tolist() / total)
    r.write("%2.3f,%2.3f,%2.3f,%2.3f,%3.3f" % (loss_test,m[0],m[1],m[2],test_accuracy))
    r.close()

    writer.close()


def SaveMeanStd():
    interestingColumns = ['SysCallRate', 'CPU%', 'IdleCPU%', 'PkFSSp%', 'CacheRdRt', 'Memory%', 'UserMem%', 'PgOutRate',
                          'PageOut', 'Sys+Cache%', 'SysMem%', 'InPktRate', 'OutPktRate', 'NetworkPktRt', 'AliveProc',
                          'ActiveProc']
    df = list()
    df.append(pd.read_csv("VMdata/SQL1PRO.data", sep='\t', usecols=interestingColumns))
    df.append(pd.read_csv("VMdata/SQL2PRO.data", sep='\t', usecols=interestingColumns))
    df.append(pd.read_csv("VMdata/SQL3PRO.data", sep='\t', usecols=interestingColumns))
    df.append(pd.read_csv("VMdata/SQL4PRO.data", sep='\t', usecols=interestingColumns))
    df.append(pd.read_csv("VMdata/WEB1PRO.data", sep='\t', usecols=interestingColumns))
    df.append(pd.read_csv("VMdata/WEB2PRO.data", sep='\t', usecols=interestingColumns))
    df.append(pd.read_csv("VMdata/WEB3PRO.data", sep='\t', usecols=interestingColumns))
    df.append(pd.read_csv("VMdata/WEB4PRO.data", sep='\t', usecols=interestingColumns))
    data = pd.concat(df)
    data = data.reset_index(drop=True)
    print("data shape total: ", data.shape)

    mean = data.mean()
    std = data.std()

    meanstd = pd.concat([mean, std], axis=1)
    meanstd.columns = ['mean','std']
    # meanstd.to_csv('meanStd.csv')


if __name__ == '__main__':
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    global args
    args = parser.parse_args()

    data, channels = load_chunks(normalize=True)
    train, val, test = split(data, train_fraction=0.7, val_fraction=0.2, test_fraction=0.1)

    training(train, val, test, channels=channels)
