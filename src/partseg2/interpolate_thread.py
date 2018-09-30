from PyQt5.QtCore import QThread, QObject, pyqtSignal
import numpy as np
from scipy.ndimage import zoom


class InterpolateThread(QThread):
    # finished = pyqtSignal(list)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.scaling = None
        self.arrays = None
        self.result = None

    def set_scaling(self, scaling):
        self.scaling = scaling

    def set_arrays(self, arrays_list):
        self.arrays = arrays_list

    def run(self):
        self.result = []
        for el in self.arrays:
            if len(el.shape) == len(self.scaling):
                self.result.append(zoom(el, self.scaling))
            else:
                shape = [int(x*y) for x,y in zip(self.scaling, el.shape)] + list(el.shape[len(self.scaling):])
                cache = np.zeros(shape, dtype=el.dtype)
                for i in range(el.shape[-1]):
                    cache[..., i] = zoom(el[..., i], self.scaling)
                self.result.append(cache)
        return