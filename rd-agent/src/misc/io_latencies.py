#!/usr/bin/python3
#
# fmt: off
#
# io_latencies.py  Monitor IO latency distribution of a block device.
#
#  $ ./io_latencies.py 259:0
#  259:0      p1    p5   p10   p16   p25   p50   p75   p84   p90   p95   p99  p100
#  read     95us 175us 305us 515us 895us 985us 995us 1.5ms 2.5ms 3.5ms 4.5ms  10ms
#  write     5us   5us   5us  15us  25us 135us 765us 855us 885us 895us 965us 1.5ms
#  discard   5us   5us   5us   5us 135us 145us 165us 205us 385us 875us 1.5ms 2.5ms
#  flush     5us   5us   5us   5us   5us   5us   5us   5us   5us 1.5ms 4.5ms 5.5ms
#
# Copyright (C) 2020 Tejun Heo <tj@kernel.org>
# Copyright (C) 2020 Facebook

from __future__ import print_function
from bcc import BPF
from time import sleep
import argparse
import json
import sys

description = """
Monitor IO latency distribution of a block device
"""

parser = argparse.ArgumentParser(description = description,
                                 formatter_class = argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('devno', metavar='MAJ:MIN', type=str,
                    help='Target block device number')
parser.add_argument('-i', '--interval', type=int, default=3,
                    help='Report interval')
parser.add_argument('-w', '--which', choices=['from-rq-alloc', 'after-rq-alloc', 'on-device'],
                    default='on-device', help='Which latency to measure')
parser.add_argument('-p', '--pcts', metavar='PCT', type=str, nargs='+',
                    default=['1', '5', '10', '16', '25', '50', '75', '84', '90', '95', '99', '100'],
                    help='Percentiles to calculate')
parser.add_argument('-j', '--json', action='store_true',
                    help='Output in json')
parser.add_argument('--verbose', '-v', action='count', default = 0)

bpf_source = """
#include <linux/blk_types.h>
#include <linux/blkdev.h>
#include <linux/time64.h>

BPF_PERCPU_ARRAY(rwdf_100ms, u64, 400);
BPF_PERCPU_ARRAY(rwdf_1ms, u64, 400);
BPF_PERCPU_ARRAY(rwdf_10us, u64, 400);

static unsigned int rq_cmd(unsigned int cmd_flags)
{
        __asm__ __volatile__("" : : : "memory");
        return cmd_flags & REQ_OP_MASK;
}

void kprobe_blk_account_io_done(struct pt_regs *ctx, struct request *rq, u64 now)
{
        u64 dur;
        size_t base, slot;

        if (!rq->__START_TIME_FIELD__)
                return;

        if (!rq->rq_disk ||
            rq->rq_disk->major != __MAJOR__ ||
            rq->rq_disk->first_minor != __MINOR__)
                return;

        /*
         * bcc chokes when the following switch block is converted to a jump
         * table. Work around by using if/else with forced memory access each
         * step.
         */
        if (rq_cmd(rq->cmd_flags) == REQ_OP_READ)
                base = 0;
        else if (rq_cmd(rq->cmd_flags) == REQ_OP_WRITE)
                base = 100;
        else if (rq_cmd(rq->cmd_flags) == REQ_OP_DISCARD)
                base = 200;
        else if (rq_cmd(rq->cmd_flags) == REQ_OP_FLUSH)
                base = 300;
        else
                return;

        dur = now - rq->__START_TIME_FIELD__;

        slot = min_t(size_t, div_u64(dur, 100 * NSEC_PER_MSEC), 99);
        rwdf_100ms.increment(base + slot);
        if (slot)
                return;

        slot = min_t(size_t, div_u64(dur, NSEC_PER_MSEC), 99);
        rwdf_1ms.increment(base + slot);
        if (slot)
                return;

        slot = min_t(size_t, div_u64(dur, 10 * NSEC_PER_USEC), 99);
        rwdf_10us.increment(base + slot);
}
"""

args = parser.parse_args()
args.pcts.sort(key=lambda x: float(x))

if args.which == 'from-rq-alloc':
    start_time_field = 'alloc_time_ns'
elif args.which == 'after-rq-alloc':
    start_time_field = 'start_time_ns'
elif args.which == 'on-device':
    start_time_field = 'io_start_time_ns'
else:
    die()

bpf_source = bpf_source.replace('__START_TIME_FIELD__', start_time_field)
bpf_source = bpf_source.replace('__MAJOR__', str(int(args.devno.split(':')[0])))
bpf_source = bpf_source.replace('__MINOR__', str(int(args.devno.split(':')[1])))

bpf = BPF(text=bpf_source)
bpf.attach_kprobe(event="blk_account_io_done", fn_name="kprobe_blk_account_io_done")

# times are in usecs
MSEC = 1000
SEC = 1000 * 1000

cur_rwdf_100ms = bpf["rwdf_100ms"]
cur_rwdf_1ms = bpf["rwdf_1ms"]
cur_rwdf_10us = bpf["rwdf_10us"]

last_rwdf_100ms = [0] * 400
last_rwdf_1ms = [0] * 400
last_rwdf_10us = [0] * 400

rwdf_100ms = [0] * 400
rwdf_1ms = [0] * 400
rwdf_10us = [0] * 400

io_type = ["read", "write", "discard", "flush"]

def find_pct(req, total, slots, idx, counted):
    while idx > 0:
        idx -= 1
        if slots[idx] > 0:
            counted += slots[idx]
            if args.verbose > 1:
                print('idx={} counted={} pct={:.1f} req={}'
                      .format(idx, counted, counted / total, req))
            if (counted / total) * 100 >= 100 - req:
                break
    return (idx, counted)

def calc_lat_pct(req_pcts, total, lat_100ms, lat_1ms, lat_10us):
    pcts = [0] * len(req_pcts)

    if total == 0:
        return pcts

    data = [(100 * MSEC, lat_100ms), (MSEC, lat_1ms), (10, lat_10us)]
    data_sel = 0
    idx = 100
    counted = 0

    for pct_idx in reversed(range(len(req_pcts))):
        req = float(req_pcts[pct_idx])
        while True:
            last_counted = counted
            (gran, slots) = data[data_sel]
            (idx, counted) = find_pct(req, total, slots, idx, counted)
            if args.verbose > 1:
                print('pct_idx={} req={} gran={} idx={} counted={} total={}'
                      .format(pct_idx, req, gran, idx, counted, total))
            if idx > 0 or data_sel == len(data) - 1:
                break
            counted = last_counted
            data_sel += 1
            idx = 100

        pcts[pct_idx] = gran * idx + gran / 2

    return pcts

def format_usec(lat):
    if lat > SEC:
        return '{:.1f}s'.format(lat / SEC)
    elif lat > 10 * MSEC:
        return '{:.0f}ms'.format(lat / MSEC)
    elif lat > MSEC:
        return '{:.1f}ms'.format(lat / MSEC)
    elif lat > 0:
        return '{:.0f}us'.format(lat)
    else:
        return '-'

# 0 interval can be used to test whether this script would run successfully.
if args.interval == 0:
    sys.exit(0)

while True:
    sleep(args.interval)

    rwdf_total = [0] * 4;

    for i in range(400):
        v = cur_rwdf_100ms.sum(i).value
        rwdf_100ms[i] = max(v - last_rwdf_100ms[i], 0)
        last_rwdf_100ms[i] = v

        v = cur_rwdf_1ms.sum(i).value
        rwdf_1ms[i] = max(v - last_rwdf_1ms[i], 0)
        last_rwdf_1ms[i] = v

        v = cur_rwdf_10us.sum(i).value
        rwdf_10us[i] = max(v - last_rwdf_10us[i], 0)
        last_rwdf_10us[i] = v

        rwdf_total[int(i / 100)] += rwdf_100ms[i]

    rwdf_lat = []
    for i in range(4):
        left = i * 100
        right = left + 100
        rwdf_lat.append(
            calc_lat_pct(args.pcts, rwdf_total[i],
                         rwdf_100ms[left:right],
                         rwdf_1ms[left:right],
                         rwdf_10us[left:right]))

        if args.verbose:
            print('{:7} 100ms {}'.format(io_type[i], rwdf_100ms[left:right]))
            print('{:7}   1ms {}'.format(io_type[i], rwdf_1ms[left:right]))
            print('{:7}  10us {}'.format(io_type[i], rwdf_10us[left:right]))

    if args.json:
        result = {}
        for iot in range(4):
            lats = {}
            for pi in range(len(args.pcts)):
                lats[args.pcts[pi]] = rwdf_lat[iot][pi] / SEC
            result[io_type[iot]] = lats
        print(json.dumps(result), flush=True)
    else:
        print('\n{:<7}'.format(args.devno), end='')
        widths = []
        for pct in args.pcts:
            widths.append(max(len(pct), 5))
            print(' {:>5}'.format(pct), end='')
        print()
        for iot in range(4):
            print('{:7}'.format(io_type[iot]), end='')
            for pi in range(len(rwdf_lat[iot])):
                print(' {:>{}}'.format(format_usec(rwdf_lat[iot][pi]), widths[pi]), end='')
            print()
