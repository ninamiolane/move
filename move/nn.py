# Modified from
# https://towardsdatascience.com/implementation-differences-in-lstm-layers-tensorflow-vs-pytorch-77a31d742f74
import math
import os
from glob import glob

import numpy as np
import torch
import torch.nn as nn
from torch.nn import init
from torch.autograd import Variable
import wandb
from matplotlib import animation

from config import *
from artifact import *



class LstmEncoder(torch.nn.Module):
    def __init__(
        self, n_layers=2, input_features=3 * 53, h_features_loop=32, latent_dim=32
    ):
        super().__init__()

        self.n_layers = n_layers
        self.lstm1 = torch.nn.LSTM(
            input_size=input_features, hidden_size=h_features_loop, batch_first=True
        )
        self.lstm2 = torch.nn.LSTM(
            input_size=h_features_loop, hidden_size=h_features_loop, batch_first=True
        )
        self.mean_block = torch.nn.Linear(h_features_loop, latent_dim)
        self.logvar_block = torch.nn.Linear(h_features_loop, latent_dim)

    def reparametrize(self, z_mean, z_logvar):
        # # print("reparametrize function called")
        std = torch.exp(0.5 * z_logvar)
        # print("made std")
        eps = torch.randn_like(std)
        # print("# print made eps")
        return eps.mul(std).add_(z_mean)

    def forward(self, inputs):
        """Note that:
        inputs has shape=[batch_size, seq_len, input_features].
        """
        # print("starting the forward of encoder. the first step is calling layer lstm1")
        # print("input to encoder should have [8,128,159]")
        # print(inputs.shape)
        h1, (h1_T, c1_T) = self.lstm1(inputs)
        # print('h after linear encoder layer')
        # print(h1.shape)
        # print(
        #     "done layer lstm1."
        #     " It returned h1 of shape {} and h1_T of shape{}".format(
        #         h1.shape, h1_T.shape
        #     )
        # )

        # print("Now starting the loop of {}-1 lstm layers".format(self.n_layers))
        for i in range(self.n_layers - 1):
            # print("this is loop iteration {}. Calling layer lstm2".format(i))
            h1, (h1_T, c1_T) = self.lstm2(h1)
            # print(' h and last h of second lstm encoder')
            # print(h1.shape, h1_T.shape)

            # # print(
            #     "done layer lstm 2. "
            #     "lstm2 returns h2 of shape {} and h2_T of shape {}".format(
            #         h2.shape, h2_T.shape
            #     )
            # )

        # print("Now computing the encoder output.")
        # print("calling mean_block")
        h1_T_batchfirst = h1_T.squeeze(axis=0)       
        z_mean = self.mean_block(h1_T_batchfirst)
        z_logvar = self.logvar_block(h1_T_batchfirst)
        z_sample = self.reparametrize(z_mean, z_logvar)

        # print("encoder is done")
        return z_sample, z_mean, z_logvar


class LstmDecoder(torch.nn.Module):
    def __init__(
        self,
        n_layers=2,
        output_features=3 * 53,
        h_features_loop=32,
        latent_dim=32,
        seq_len=128,
        negative_slope=0.2,
    ):
        super().__init__()
        self.latent_dim = latent_dim
        self.seq_len = seq_len
        self.n_layers = n_layers

        self.linear = torch.nn.Linear(latent_dim, h_features_loop)
        self.leakyrelu = torch.nn.LeakyReLU(negative_slope=negative_slope)

        self.lstm_loop = torch.nn.LSTM(
            input_size=h_features_loop, hidden_size=h_features_loop, batch_first=True
        )

        self.lstm2 = torch.nn.LSTM(
            input_size=h_features_loop, hidden_size=output_features, batch_first=True
        )

    def forward(self, inputs):

        h = self.linear(inputs)

        h = self.leakyrelu(h)

        #assert len(h.shape) == 2, h.shape
        h = h.reshape((h.shape[0], 1, h.shape[-1]))  # ,self.seq_len, 1, 1)

        h = h.repeat(1, self.seq_len, 1)

        for i in range(self.n_layers - 1):
            h, (h_T, c_T) = self.lstm_loop(h)

        h, (h_T, c_T) = self.lstm2(h)

        return h


def log_gaussian(x, mu, log_var):
    """
    Returns the log pdf of normal distribution parametrised
    by mu and log_var evaluated at x.
    :param x: point to evaluate
    :param mu: mean of distribution
    :param log_var: log variance of distribution
    :return: log N(x|µ,σ)
    """
    log_pdf = (
        -0.5 * math.log(2 * math.pi)
        - log_var / 2
        - (x - mu) ** 2 / (2 * torch.exp(log_var))
    )
    return torch.sum(log_pdf, dim=-1)


def log_standard_gaussian(x):
    """
    Evaluates the log pdf of a standard normal distribution at x.
    :param x: point to evaluate
    :return: log N(x|0,I)
    """
    return torch.sum(-0.5 * math.log(2 * math.pi) - x ** 2 / 2, dim=-1)


class LstmVAE(torch.nn.Module):
    def __init__(self, input_features=3 * 53) : #, h_features_loop):
        """
        Variational Autoencoder model
        consisting of an (LSTM+encoder)/(decoder+LSTM) pair.
        :param dims: x, z and hidden dimensions of the networks
        """
        super(LstmVAE, self).__init__()

        self.encoder = LstmEncoder(input_features=input_features)  # , h_feature_loop=...
        self.decoder = LstmDecoder()
        self.kl_divergence = 0

        for m in self.modules():
            if isinstance(m, nn.Linear):
                init.xavier_normal(m.weight.data)  # initialize weight W
                if m.bias is not None:  # initialize b in W*x+b
                    m.bias.data.zero_()

    def _kld(self, z, q_param, p_param=None):
        """
        Computes the KL-divergence of
        some element z.
        KL(q||p) = -∫ q(z) log [ p(z) / q(z) ]
                  = -E[log p(z) - log q(z)]
        :param z: sample from q-distribuion
        :param q_param: (mu, log_var) of the q-distribution
        :param p_param: (mu, log_var) of the p-distribution
        :return: KL(q||p)
        """
        # -0.5*K.mean(K.sum(1 + auto_log_var -
        # K.square(auto_mean) - K.exp(auto_log_var), axis=-1))

        (mu, log_var) = q_param

        kl = -(torch.sum(1 + log_var - torch.square(mu) - log_var.exp(), axis=-1))

        return kl

    def elbo(self, x_in, x_out, z, q_param, p_param=None):
        # print(
        #     "x_in has shape {}"
        #     " and x_out has shape {}".format(x_in.shape, x_out.shape)
        # )
        recon_loss = torch.sum(torch.norm((x_in - x_out)) ** 2)
        # x_out is sequence
        # 0.5*K.mean(K.sum(K.square(auto_input - auto_output), axis=-1))

        regul_loss = self._kld(z, q_param, p_param)
        # -0.5*K.mean(K.sum(
        # 1 + auto_log_var - K.square(auto_mean) - K.exp(auto_log_var), axis=-1))

        return recon_loss + regul_loss

    def add_flow(self, flow):
        self.flow = flow

    def forward(self, x, y=None):
        """
        Runs a data point through the model in order
        to provide its reconstruction and q distribution
        parameters.
        :param x: input data, has shape=[batch_size, seqlen, input_features].
        :return: reconstructed input
        """
        z_sample, z_mean, z_log_var = self.encoder(x)

        self.kl_divergence = self._kld(z_sample, (z_mean, z_log_var))

        x_mean = self.decoder(z_sample)

        return x_mean, z_sample, z_mean, z_log_var


# def setup_gpus():
#     # use tensorflow backend
#     # set random seeds
#     # tf.set_random_seed(1)
#     # np.random.seed(1)
#     # identify available GPU's
#     #     gpus = K.tensorflow_backend.
# _get_available_gpus() # works with TF 1 (?)
#     #     gpus = tf.config.experimental.list_physical_devices('GPU') # works with TF 2

#     os.environ[
#         "CUDA_VISIBLE_DEVICES"
#     ] = "3"  # pick a number < 4 on ML4HEP; < 3 on Voltan
#     gpu_options = tf.GPUOptions(
# allow_growth=True, per_process_gpu_memory_fraction=0.5)
#     sess = tf.Session(config=tf.ConfigProto(gpu_options=gpu_options))
#     # allow dynamic GPU memory allocation
#     config = tf.compat.v1.ConfigProto()
#     config.gpu_options.allow_growth = True
#     session = tf.compat.v1.Session(config=config)
#     #     # print("GPUs found: {}".format(len(gpus)))
#     return ()


def load_data(pattern="data/vae_data/mariel_*.npy"):
    # load up the six datasets, performing some minimal preprocessing beforehand
    datasets = {}
    ds_all = []

    exclude_points = [26, 53]
    point_mask = np.ones(55, dtype=bool)
    point_mask[exclude_points] = 0

    for f in sorted(glob(pattern)):
        ds_name = os.path.basename(f)[7:-4]
        # print("loading:", ds_name)
        ds = np.load(f).transpose((1, 0, 2))
        ds = ds[500:-500, point_mask]
        # print("\t Shape:", ds.shape)

        ds[:, :, 2] *= -1
        # print("\t Min:", np.min(ds, axis=(0, 1)))
        # print("\t Max:", np.max(ds, axis=(0, 1)))

        # ds = filter_points(ds)

        datasets[ds_name] = ds
        ds_all.append(ds)

    ds_counts = np.array([ds.shape[0] for ds in ds_all])
    ds_offsets = np.zeros_like(ds_counts)
    ds_offsets[1:] = np.cumsum(ds_counts[:-1])

    ds_all = np.concatenate(ds_all)
    # print("Full data shape:", ds_all.shape)
    # # print("Offsets:", ds_offsets)

    # # print(ds_all.min(axis=(0,1)))
    low, hi = np.quantile(ds_all, [0.01, 0.99], axis=(0, 1))
    xy_min = min(low[:2])
    xy_max = max(hi[:2])
    xy_range = xy_max - xy_min
    ds_all[:, :, :2] -= xy_min
    ds_all *= 2 / xy_range
    ds_all[:, :, :2] -= 1.0

    # it's also useful to have these datasets centered, i.e. with the x and y offsets
    # subtracted from each individual frame

    ds_all_centered = ds_all.copy()
    ds_all_centered[:, :, :2] -= ds_all_centered[:, :, :2].mean(axis=1, keepdims=True)

    datasets_centered = {}
    for ds in datasets:
        datasets[ds][:, :, :2] -= xy_min
        datasets[ds] *= 2 / xy_range
        datasets[ds][:, :, :2] -= 1.0
        datasets_centered[ds] = datasets[ds].copy()
        datasets_centered[ds][:, :, :2] -= datasets[ds][:, :, :2].mean(
            axis=1, keepdims=True
        )

    # # print(ds_all.min(axis=(0,1)))
    low, hi = np.quantile(ds_all, [0.01, 0.99], axis=(0, 1))
    return ds_all, ds_all_centered, datasets, datasets_centered, ds_counts

########################################################################
#TRAINING FUNCTIONS

def get_loss(model, x, x_recon, z, z_mu, z_logvar):
    loss = torch.mean(model.elbo(x, x_recon, z, (z_mu, z_logvar)))
    return loss


def run_train(model, data_train_torch, data_valid_torch, data_test_torch, get_loss, optimizer, epochs):

    # Run training and track with wandb
    example_ct = 0  # number of examples seen
    batch_ct = 0
    example_ct_valid = 0  # number of examples seen
    batch_ct_valid = 0
    for epoch in range(epochs):

        #Train
        model = model.train()

        loss_epoch = 0
        for x in data_train_torch:
            x = Variable(x)
            x = x.to(device)

            loss = train_batch(x, model, optimizer, get_loss)
            loss_epoch += loss

            example_ct +=  len(x) #add amount of examples in 1 batch
            batch_ct += 1

            # Report metrics every 25th batch
            if ((batch_ct + 1) % 25) == 0:
                train_log(loss, example_ct, epoch)

        loss_epoch /= batch_ct # get average loss/epoch

        #Run Validation
        model = model.eval()

        loss_valid_epoch = 0
        for x in data_valid_torch:
            x = Variable(x)
            x = x.to(device)

            loss_valid = valid_batch(x, model, get_loss) #same as before, except no back propogation
            loss_valid_epoch += loss_valid

            example_ct_valid +=  len(x) #add amount of examples in 1 batch
            batch_ct_valid += 1

            # Report metrics every 25th batch
            if ((batch_ct_valid + 1) % 25) == 0:
                valid_log(loss_valid, example_ct_valid, epoch)            
        
        #Run testing
        #Make and log artifact at the end of each epoch (stick-figure video)
        index_of_chosen_seq = np.random.randint(0,data_test_torch.dataset.shape[0])
        print('INDEX OF TESTING SEQUENCE IS {}'.format(index_of_chosen_seq))
        i = 0
        for x in data_test_torch:
            i += 1

            if i == index_of_chosen_seq:
                print('Found test sequence. Running it through model')
                x = Variable(x)
                x = x.to(device)
                x_input = x
                x_recon, z, z_mu, z_logvar = model(x.float())
                print('Ran it through model')

            else:            
                pass

        x_input_formatted = x_input.reshape((128,53,3))
        x_recon_formatted = x_recon.reshape((128,53,3))

        anim = animate_stick(x_input_formatted, epoch=epoch, index=index_of_chosen_seq, ghost=x_recon_formatted, dot_alpha=0.7, ghost_shift=0.2, figsize=(12,8))
        print('Called animation function for epoch {}'.format(epoch+1))

    print("done training")


def train_batch(x, model, optimizer, get_loss):    
    
    #Forward pass
    x_recon, z, z_mu, z_logvar = model(x.float())
    #x_recon_batch_first=x_recon.reshape((x_recon.shape[1], x_recon.shape[0],x_recon.shape[2]))
    loss = get_loss(model, x, x_recon, z, z_mu, z_logvar)

    #Backward pass
    optimizer.zero_grad()
    loss.backward()

    #Optimizer takes step
    optimizer.step()

    return loss

def train_log(loss, example_ct, epoch):
    # Where the magic happens
    wandb.log({"epoch": epoch, "loss": loss}, step=example_ct)
    print('Loss after {} examples: {}'.format(str(example_ct).zfill(5), loss))

def valid_batch(x, model, get_loss):    

    #Forward pass
    x_recon, z, z_mu, z_logvar = model(x.float())
    #x_recon_batch_first=x_recon.reshape((x_recon.shape[1], x_recon.shape[0],x_recon.shape[2]))
    valid_loss = get_loss(model, x, x_recon, z, z_mu, z_logvar)

    return valid_loss

def valid_log(valid_loss, example_ct, epoch):
    # Where the magic happens
    wandb.log({"valid_loss": valid_loss})
