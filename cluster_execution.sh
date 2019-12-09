#!/bin/sh
#PBS -l select=1:ngpus=1
#PBS -q gpuq_cuda10
#PBS -N relu-TL

cd $PBS_O_WORKDIR
. ~/anaconda3/etc/profile.d/conda.sh
conda activate gpu

python main.py pre-train && python main.py transfer-learning