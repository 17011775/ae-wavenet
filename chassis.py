from sys import stderr
import torch as t
from torch.utils.tensorboard import SummaryWriter
import data
import autoencoder_model as ae
import mfcc_inverter as mi
import checkpoint as ckpt
import util
import netmisc
import librosa
import os.path
import time

try:
    import torch_xla
    import torch_xla.core.xla_model as xm
    import torch_xla.distributed.parallel_loader as pl
except ModuleNotFoundError:
    pass


class GPULoaderIter(object):
    def __init__(self, loader, device):
        self.loader_iter = iter(loader)
        self.device = device

    def __iter__(self):
        return self

    def __next__(self):
        items = next(self.loader_iter)
        return tuple(item.to(self.device) for item in items)


def reduce_add(vlist):
    return t.stack(vlist).sum(dim=0)

def reduce_mean(vlist):
    return t.stack(vlist).mean(dim=0)

class Chassis(object):
    """
    Coordinates the construction of the model, dataset, optimizer,
    checkpointing state, and GPU/TPU iterator wrappers.

    Provides a single function for training the model from the constructed
    setup. 

    """
    def __init__(self, device, index, hps, dat_file):
        self.is_tpu = (hps.hw in ('TPU', 'TPU-single'))
        if self.is_tpu:
            num_replicas = xm.xrt_world_size()
            rank = xm.get_ordinal()
        elif hps.hw == 'GPU':
            if not t.cuda.is_available():
                raise RuntimeError('GPU requested but not available')
            num_replicas = 1
            rank = 0
        elif hps.hw == 'CPU':
            num_replicas = 1
            rank = 0
        else:
            raise ValueError(f'Chassis: Invalid device "{hps.hw}" requested')

        self.replica_index = index

        self.state = ckpt.Checkpoint(hps, dat_file, train_mode=True,
                ckpt_file=hps.get('ckpt_file', None),
                num_replicas=num_replicas, rank=rank)

        hps = self.state.hps
        if not self.is_tpu or xm.is_master_ordinal():
            print('Hyperparameters:\n', file=stderr)
            print('\n'.join(f'{k} = {v}' for k, v in hps.items()), file=stderr)

        self.learning_rates = dict(zip(hps.learning_rate_steps,
            hps.learning_rate_rates))

        if self.state.model.bn_type == 'vae':
            self.anneal_schedule = dict(zip(hps.bn_anneal_weight_steps,
                hps.bn_anneal_weight_vals))

        self.ckpt_path = util.CheckpointPath(hps.ckpt_template, not self.is_tpu
                or xm.is_master_ordinal())

        self.softmax = t.nn.Softmax(1) # input to this is (B, Q, N)
        self.hw = hps.hw

        if hps.hw == 'GPU':
            self.device_loader = GPULoaderIter(self.state.data.loader, device)
            self.state.to(device)
        else:
            para_loader = pl.ParallelLoader(self.state.data.loader, [device])
            self.device_loader = para_loader.per_device_loader(device) 
            self.num_devices = xm.xrt_world_size()
            self.state.to(device)

        self.state.init_torch_generator()
         
        if not self.is_tpu or xm.is_master_ordinal():
            print(f'attempting to create writer to {hps.log_dir}', file=stderr, flush=True)
            try:
                self.writer = SummaryWriter(f'{hps.log_dir}')
            except:
                print('could not create SummaryWriter', file=stderr,
                        flush=True)
            print(f'created SummaryWriter to {hps.log_dir}', file=stderr,
                    flush=True)
        else:
            self.writer = None

    def train(self):
        hps = self.state.hps
        ss = self.state 
        current_stats = {}

        # for resuming the learning rate 
        sorted_lr_steps = sorted(self.learning_rates.keys())
        lr_index = util.greatest_lower_bound(sorted_lr_steps, ss.data.global_step)
        ss.update_learning_rate(self.learning_rates[sorted_lr_steps[lr_index]])

        if ss.model.bn_type != 'none':
            sorted_as_steps = sorted(self.anneal_schedule.keys())
            as_index = util.greatest_lower_bound(sorted_as_steps,
                    ss.data.global_step)
            ss.model.objective.update_anneal_weight(self.anneal_schedule[sorted_as_steps[as_index]])

        if ss.model.bn_type in ('vqvae', 'vqvae-ema'):
            ss.model.init_codebook(self.data_iter, 10000)

        start_time = time.time()
        
        for batch_num, batch in enumerate(self.device_loader):
            wav, mel, voice, jitter, position = batch
            global_step = len(ss.data.dataset) * position[0] + position[1]

            # print(f'replica {self.replica_index}, batch {batch_num}', file=stderr)
            # stderr.flush()
            if (batch_num % hps.save_interval == 0 and batch_num != 0):
                self.save_checkpoint(position)

            if hps.skip_loop_body:
                continue

            if ss.data.global_step in self.learning_rates:
                ss.update_learning_rate(self.learning_rates[ss.data.global_step])

            if ss.model.bn_type == 'vae' and ss.step in self.anneal_schedule:
                ss.model.objective.update_anneal_weight(self.anneal_schedule[ss.data.global_step])

            ss.optim.zero_grad()
            quant, self.target, loss = self.state.model.run(wav, mel, voice, jitter) 
            self.probs = self.softmax(quant)
            self.mel_enc_input = mel
            # print(f'after model.run', file=stderr)
            # stderr.flush()
            loss.backward()

            # print(f'after loss.backward()', file=stderr)
            # stderr.flush()

            if batch_num % hps.progress_interval == 0:
                pars_copy = [p.data.clone() for p in ss.model.parameters()]
                
            # print(f'after pars_copy', file=stderr)
            # stderr.flush()

            if self.is_tpu:
                xm.optimizer_step(ss.optim)
            else:
                ss.optim.step()

            ss.optim_step += 1

            if ss.model.bn_type == 'vqvae-ema' and ss.data.global_step == 10000:
                ss.model.bottleneck.update_codebook()

            tprb_m = self.avg_prob_target()

            if batch_num % hps.progress_interval == 0:
                iterator = zip(pars_copy, ss.model.named_parameters())
                updates = t.stack([t.norm(c - np[1].data) for c, np in iterator])
                original = t.stack([p.norm() for p in pars_copy])
                uw_ratio = updates / original

                current_stats.update({
                       'uwr_min': uw_ratio.min(),
                       'uwr_max': uw_ratio.max()
                       })

                if self.is_tpu:
                    loss_red = xm.mesh_reduce('mesh_loss', loss, reduce_mean)
                    tprb_red = xm.mesh_reduce('mesh_tprb', tprb_m, reduce_mean)
                else:
                    loss_red = loss
                    tprb_red = tprb_m

                current_stats.update({ 'loss_r': loss_red })
                current_stats.update({ 'tprb_r': tprb_red })

                current_stats.update({
                        'optim_step': ss.optim_step,
                        'gstep': len(ss.data.dataset) * position[0] + position[1],
                        'epoch': position[0],
                        'step': position[1],
                        'loss': loss,
                        'lrate': ss.optim.param_groups[0]['lr'],
                        # 'tprb_m': tprb_m,
                        # 'pk_d_m': avg_peak_dist
                        })
                current_stats.update(ss.model.objective.metrics)

                if ss.model.bn_type in ('vae'):
                    current_stats['free_nats'] = ss.model.objective.free_nats
                    current_stats['anneal_weight'] = \
                            ss.model.objective.anneal_weight.item()

                if ss.model.bn_type in ('vqvae', 'vqvae-ema', 'ae', 'vae'):
                    current_stats.update(ss.model.encoder.metrics)

                if self.is_tpu:
                    xm.add_step_closure(
                            self.train_update,
                            args=(current_stats))
                else:
                    self.train_update(current_stats)

                # if not self.is_tpu or xm.is_master_ordinal():
                # if batch_num in range(25, 50) or batch_num in range(75, 100):
                stderr.flush()
                elapsed = time.time() - start_time
                # print(f'{elapsed}, worker {self.replica_index}, batch {batch_num}', file=stderr)
                # stderr.flush()

    def train_update(self, stats):
        netmisc.print_metrics(stats, self.replica_index, 100)
        if self.writer:
            self.writer.add_scalars('metrics', { k: stats[k].item() for k
                in ('loss_r', 'tprb_r') }, stats['optim_step'])

            self.writer.add_scalars('uwr', { k: stats[k].item() for k
                in ('uwr_min', 'uwr_max') }, stats['optim_step'])

        
    def save_checkpoint(self, position):
        global_step = len(self.state.data.dataset) * position[0] + position[1]
        ckpt_file = self.ckpt_path.path(global_step.item())
        self.state.save(ckpt_file, position[0], position[1])
        
        if not self.is_tpu or xm.is_master_ordinal():
            print('Saved checkpoint to {}'.format(ckpt_file), file=stderr)
            stderr.flush()

    def avg_max(self):
        """Average max value for the predictions.  As the prediction becomes
        more peaked, this should go up"""
        max_val, max_ind = t.max(self.probs, dim=1)
        mean = t.mean(max_val)
        return mean
        
    def avg_prob_target(self):
        """Average probability given to target"""
        target_probs = t.gather(self.probs, 1, self.target.long().unsqueeze(1)) 
        mean = t.mean(target_probs)
        return mean


class DataContainer(t.nn.Module):
    def __init__(self, my_values):
        super().__init__()
        for key in my_values:
            setattr(self, key, my_values[key])

    def forward(self):
        pass


class InferenceChassis(object):
    """
    Coordinates construction of model and dataset for running inference
    """
    def __init__(self, mode, opts):
        self.output_dir = opts.output_dir
        self.n_replicas = opts.dec_n_replicas
        self.data_write_tmpl = opts.data_write_tmpl

        self.state = ckpt.InferenceState()
        self.state.load(opts.ckpt_file, opts.dat_file)
        self.state.model.wavenet.set_n_replicas(self.n_replicas)
        self.state.model.eval()

        if opts.hwtype in ('GPU', 'CPU'):
            if opts.hwtype == 'GPU':
                self.device = t.device('cuda')
            else:
                self.device = t.device('cpu')
            self.data_loader = self.state.data_loader
            self.data_loader.set_target_device(self.device)
            self.data_iter = GPULoaderIter(iter(self.data_loader))
        else:
            import torch_xla.core.xla_model as xm
            import torch_xla.distributed.parallel_loader as pl
            self.device = xm.xla_device()
            self.data_loader = pl.ParallelLoader(self.state.data_loader, [self.device])
            self.data_iter = TPULoaderIter(self.data_loader, self.device)

    def infer(self, model_scr=None):
        self.state.to(self.device)
        sample_rate = self.state.data_loader.dataset.sample_rate
        n_quant = self.state.model.wavenet.n_quant

        for vb in self.data_iter:
            if self.data_write_tmpl:
                dc = t.jit.script(DataContainer({
                    'mel': vb.mel,
                    'wav': vb.wav,
                    'voice': vb.voice_idx,
                    'jitter': vb.jitter_idx
                    }))
                dc.save(self.data_write_tmpl)
                print('saved {}'.format(self.data_write_tmpl))

            out_template = os.path.join(self.output_dir,
                    os.path.basename(os.path.splitext(vb.file_path)[0])
                    + '.{}.wav')

            if model_scr:
                with t.no_grad():
                    wav = model_scr(vb)
            else:
                wav = self.state.model(vb)

            wav_orig, wav_sample = wav[0,...], wav[1:,...]

            # save results to specified files
            for i in range(self.n_replicas):
                wav_final = util.mu_decode_torch(wav_sample[i], n_quant)
                path = out_template.format('rep' + str(i)) 
                librosa.output.write_wav(path, wav_final.cpu().numpy(), sample_rate) 

            wav_final = util.mu_decode_torch(wav_orig, n_quant)
            path = out_template.format('orig') 
            librosa.output.write_wav(path, wav_final.cpu().numpy(), sample_rate) 

            print('Wrote {}'.format(
                out_template.format('0-'+str(self.n_replicas-1))))

