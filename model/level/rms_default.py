import numpy as np


def rms_level(data):
    rms = np.sqrt(np.mean(np.square(data)))
    return min(rms * 10, 1.0)
