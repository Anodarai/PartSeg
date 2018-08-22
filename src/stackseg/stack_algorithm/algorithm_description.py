from abc import ABCMeta, abstractmethod
from os import path
from typing import Type

import numpy as np
import tifffile
from PyQt5.QtWidgets import QSpinBox, QDoubleSpinBox, QComboBox, QCheckBox, QWidget, QVBoxLayout, QLabel, QFormLayout, \
    QAbstractSpinBox, QScrollArea
from six import with_metaclass

from partseg.io_functions import save_stack_segmentation, load_stack_segmentation
#from qt_import import QDoubleSpinBox, QSpinBox, QComboBox, QWidget, QFormLayout, QAbstractSpinBox, QCheckBox, QThread, \
#    pyqtSignal, QLabel, QVBoxLayout
from project_utils.settings import ImageSettings
from .threshold_algorithm import ThresholdAlgorithm, ThresholdPreview, SegmentationAlgorithm, \
    AutoThresholdAlgorithm
from PyQt5.QtCore import QThread, pyqtSignal

class AlgorithmProperty(object):
    """
    :type name: str
    :type value_type: type
    :type default_value: object
    """

    def __init__(self, name, user_name, default_value, options_range, single_steep=None):
        self.name = name
        self.user_name = user_name
        self.value_type = type(default_value)
        self.default_value = default_value
        self.range = options_range
        self.single_step = single_steep
        if self.value_type is list:
            assert default_value in options_range


class BatchProceed(QThread):
    error_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(str, int)
    execution_done = pyqtSignal()

    def __init__(self):
        super(BatchProceed, self).__init__()
        self.algorithm = None
        self.parameters = None
        self.file_list = []
        self.base_file = ""
        self.components = []
        self.index = 0
        self.result_dir = ""
        self.channel_num = 0

    def set_parameters(self, algorithm: Type[SegmentationAlgorithm], parameters, channel_num, file_list, result_dir):
        self.algorithm = algorithm()
        self.parameters = parameters
        self.file_list = list(sorted(file_list))
        self.result_dir = result_dir
        self.channel_num = channel_num
        self.algorithm.execution_done.connect(self.calc_one_finished)
        self.algorithm.progress_signal.connect(self.progress_info)

    def progress_info(self, text, num):
        name = path.basename(self.file_list[self.index])
        self.progress_signal.emit("file {} ({}): {}".format(self.index+1, name, text), self.index)

    def calc_one_finished(self, segmentation):
        print("Step finished", self.index)
        name = path.basename(self.file_list[self.index])
        name = path.splitext(name)[0]+".seg"
        save_stack_segmentation(path.join(self.result_dir, name), segmentation,
                                list(range(1, len(self.components)+1)), self.base_file)
        self.index += 1
        self.run_calculation()

    def run_calculation(self):
        temp_settings = ImageSettings()
        while self.index < len(self.file_list):
            file_path = self.file_list[self.index]
            try:
                if path.splitext(file_path)[1] == ".seg":
                    segmentation, metadata = load_stack_segmentation(file_path)
                    if "base_file" not in metadata or not path.exists(metadata["base_file"]):
                        self.index += 1
                        self.error_signal.emit("not found base file for {}".format(file_path))
                        continue
                    self.base_file = metadata["base_file"]
                    self.components = metadata["components"]
                    if len(self.components) > 250:
                        blank = np.zeros(segmentation.shape, dtype=np.uint16)
                    else:
                        blank = np.zeros(segmentation.shape, dtype=np.uint8)
                    for i, v in enumerate(self.components):
                        blank[segmentation == v] = i + 1
                else:
                    self.base_file = file_path
                    self.components = []
                    blank = None
                temp_settings.image = tifffile.imread(self.base_file)
                self.algorithm.set_parameters(image=temp_settings.image[..., self.channel_num], exclude_mask=blank,
                                              **self.parameters)
                self.algorithm.start()
                break
            except Exception as e:
                self.error_signal.emit("Exception occurred during proceed {}. Exception info {}".format(file_path, e))
                self.index += 1
        if self.index >= len(self.file_list):
            self.execution_done.emit()

    def run(self):
        self.index = 0
        self.run_calculation()


class QtAlgorithmProperty(AlgorithmProperty):
    qt_class_dict = {int: QSpinBox, float: QDoubleSpinBox, list: QComboBox, bool: QCheckBox}

    def __init__(self, *args, **kwargs):
        super(QtAlgorithmProperty, self).__init__(*args, **kwargs)

    @classmethod
    def from_algorithm_property(cls, ob):
        """
        :type ob: AlgorithmProperty
        :param ob: AlgorithmProperty object
        :return: QtAlgorithmProperty
        """
        return cls(name=ob.name, user_name=ob.user_name, default_value=ob.default_value, options_range=ob.range,
                   single_steep=ob.single_step)

    def get_field(self):
        field = self.qt_class_dict[self.value_type]()
        if isinstance(field, QComboBox):
            field.addItems(self.range)
            field.setCurrentIndex(self.range.index(self.default_value))
        elif isinstance(field, QCheckBox):
            field.setChecked(self.default_value)
        else:
            field.setRange(*self.range)
            field.setValue(self.default_value)
            if self.single_step is not None:
                field.setSingleStep(self.single_step)
        return field


class AbstractAlgorithmSettingsWidget(with_metaclass(ABCMeta, object)):
    def __init__(self):
        pass

    @abstractmethod
    def get_values(self):
        """
        :return: dict[str, object]
        """
        return dict()


class AlgorithmSettingsWidget(QScrollArea):
    def __init__(self, settings, name, element_list, algorithm: Type[SegmentationAlgorithm]):
        """
        For algorithm which works on one channel
        :type settings: ImageSettings
        :param element_list:
        :param settings:
        """
        super(AlgorithmSettingsWidget, self).__init__()
        self.widget_list = []
        self.name = name
        main_layout = QVBoxLayout()
        self.info_label = QLabel()
        self.info_label.setHidden(True)
        main_layout.addWidget(self.info_label)
        widget_layout = QFormLayout()
        self.channels_chose = QComboBox()
        widget_layout.addRow("Channel", self.channels_chose)
        element_list = map(QtAlgorithmProperty.from_algorithm_property, element_list)
        for el in element_list:
            self.widget_list.append((el.name, el.get_field()))
            widget_layout.addRow(el.user_name, self.widget_list[-1][-1])
        """scroll_area = QScrollArea()
        ww = QWidget()
        ww.setLayout(widget_layout)

        scroll_area.setWidget(ww)
        #scroll_area.setStyleSheet("background-color: green")
        #main_layout.addLayout(widget_layout)
        main_layout.addWidget(scroll_area)"""
        ww = QWidget()
        ww.setLayout(widget_layout)
        #self.setLayout(main_layout)
        self.setWidget(ww)
        self.settings = settings
        value_dict = self.settings.get(f"algorithms.{self.name}", {})
        self.set_values(value_dict)
        self.settings.image_changed[int].connect(self.image_changed)
        self.algorithm = algorithm()
        self.algorithm.info_signal.connect(self.show_info)

    def show_info(self, text):
        self.info_label.setText(text)
        self.info_label.setVisible(True)

    def image_changed(self, channels_num):
        ind = self.channels_chose.currentIndex()
        self.channels_chose.clear()
        self.channels_chose.addItems(map(str, range(channels_num)))
        if ind < 0 or ind > channels_num:
            ind = 0
        self.channels_chose.setCurrentIndex(ind)

    def set_values(self, values_dict):
        for name, el in self.widget_list:
            if name not in values_dict:
                continue
            if isinstance(el, QComboBox):
                el.setCurrentText(values_dict[name])
            elif isinstance(el, QAbstractSpinBox):
                el.setValue(values_dict[name])
            elif isinstance(el, QCheckBox):
                el.setChecked(values_dict[name])
            else:
                raise ValueError("unsuported type {}".format(type(el)))

    def get_values(self):
        res = dict()
        for name, el in self.widget_list:
            if isinstance(el, QComboBox):
                res[name] = str(el.currentText())
            elif isinstance(el, QAbstractSpinBox):
                res[name] = el.value()
            elif isinstance(el, QCheckBox):
                res[name] = el.isChecked()
            else:
                raise ValueError("unsuported type {}".format(type(el)))
        return res

    def channel_num(self):
        return self.channels_chose.currentIndex()

    def execute(self, exclude_mask=None):
        values = self.get_values()
        self.algorithm.set_parameters(**{"exclude_mask": exclude_mask,
                                         "image": self.settings.get_chanel(self.channels_chose.currentIndex()),
                                         **values})
        self.settings.set(f"algorithms.{self.name}", values)
        self.algorithm.start()


AbstractAlgorithmSettingsWidget.register(AlgorithmSettingsWidget)

only_threshold_algorithm = [AlgorithmProperty("threshold", "Threshold", 1000, (0, 10 ** 6), 100),
                            AlgorithmProperty("use_gauss", "Use gauss", False, (True, False)),
                            AlgorithmProperty("gauss_radius", "Use gauss", 1.0, (0, 10), 0.1)]

threshold_algorithm = [AlgorithmProperty("threshold", "Threshold", 10000, (0, 10 ** 6), 100),
                       AlgorithmProperty("minimum_size", "Minimum size", 8000, (0, 10 ** 6), 1000),
                       AlgorithmProperty("close_holes", "Close small holes", True, (True, False)),
                       AlgorithmProperty("close_holes_size", "Small holes size", 200, (0, 10**3), 10),
                       AlgorithmProperty("smooth_border", "Smooth borders", True, (True, False)),
                       AlgorithmProperty("smooth_border_radius", "Smooth borders radius", 2, (0, 20), 1),
                       AlgorithmProperty("use_gauss", "Use gauss", False, (True, False)),
                       AlgorithmProperty("gauss_radius", "Use gauss", 1.0, (0, 10), 0.1)]

auto_threshold_algorithm = [AlgorithmProperty("suggested_size", "Suggested size", 200000, (0, 10 ** 6), 1000),
                            AlgorithmProperty("threshold", "Minimum threshold", 10000, (0, 10 ** 6), 100),
                            AlgorithmProperty("minimum_size", "Minimum size", 8000, (0, 10 ** 6), 1000),
                            AlgorithmProperty("close_holes", "Close small holes", True, (True, False)),
                            AlgorithmProperty("close_holes_size", "Small holes size", 200, (0, 10 ** 3), 10),
                            AlgorithmProperty("smooth_border", "Smooth borders", True, (True, False)),
                            AlgorithmProperty("smooth_border_radius", "Smooth borders radius", 2, (0, 20), 1),
                            AlgorithmProperty("use_gauss", "Use gauss", False, (True, False)),
                            AlgorithmProperty("gauss_radius", "Use gauss", 1.0, (0, 10), 0.1)]

stack_algorithm_dict = {
    "Threshold": (threshold_algorithm, ThresholdAlgorithm),
    "Only Threshold": (only_threshold_algorithm, ThresholdPreview),
    "Auto Threshold": (auto_threshold_algorithm, AutoThresholdAlgorithm)
}