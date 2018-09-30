import json
import logging
import os
import re
from io import BytesIO

import tifffile as tif
import numpy as np
import SimpleITK as sitk
import appdirs
from PyQt5.QtCore import Qt, QByteArray, QEvent
from PyQt5.QtGui import QIcon, QKeyEvent
from PyQt5.QtWidgets import QMainWindow, QLabel, QWidget, QPushButton, QHBoxLayout, QVBoxLayout, QGridLayout, \
    QFileDialog, QMessageBox, QCheckBox, QComboBox, QStackedLayout, QInputDialog, QDialog, QSpinBox, QAbstractSpinBox
from scipy.ndimage import zoom

from common_gui.channel_control import ChannelControl
from common_gui.stack_image_view import ColorBar
from common_gui.waiting_dialog import WaitingDialog
from partseg2.advanced_window import AdvancedWindow
from partseg2.interpolate_dialog import InterpolateDialog
from partseg2.interpolate_thread import InterpolateThread
from project_utils.algorithms_description import InteractiveAlgorithmSettingsWidget
from project_utils.global_settings import static_file_folder
from project_utils.image_operations import dilate, erode
from .partseg_settings import PartSettings, load_project, save_project, save_labeled_image, HistoryElement
from .image_view import RawImageView, ResultImageView, RawImageStack, SynchronizeView
from .algorithm_description import part_algorithm_dict

app_name = "PartSeg2"
app_lab = "LFSG"
config_folder = appdirs.user_data_dir(app_name, app_lab)


class Options(QWidget):
    def __init__(self, settings: PartSettings, channel_control2: ChannelControl,
                 left_panel: RawImageView, synchronize: SynchronizeView):
        super().__init__()
        self._settings = settings
        self.left_panel = left_panel
        self._ch_control2 = channel_control2
        self.off_left = QCheckBox("Hide left panel")
        self.off_left.stateChanged.connect(self.hide_left_panel)
        self.synchronize_checkbox = QCheckBox("Synchronize view")
        self.synchronize_checkbox.stateChanged.connect(synchronize.set_synchronize)
        self.stack_layout = QStackedLayout()
        self.algorithm_choose = QComboBox()
        self.interactive_use = QCheckBox("Interactive use")
        self.execute_btn = QPushButton("Execute")
        self.execute_btn.clicked.connect(self.execute_algorithm)
        self.save_profile_btn = QPushButton("Save segmentation profile")
        self.choose_profile = QComboBox()
        self.choose_profile.addItem("<none>")
        self.choose_profile.addItems(self._settings.get("segmentation_profiles", dict()).keys())
        self.choose_profile.setToolTip("Select profile to restore its settings")
        self.update_tooltips()
        self.choose_profile.currentTextChanged.connect(self.change_profile)
        self.interactive_use.stateChanged.connect(self.execute_btn.setDisabled)
        self.interactive_use.stateChanged.connect(self.interactive_change)
        self.save_profile_btn.clicked.connect(self.save_profile)
        widgets_list = []
        for name, val in part_algorithm_dict.items():
            self.algorithm_choose.addItem(name)
            widget = InteractiveAlgorithmSettingsWidget(settings, name, *val,
                                                        selector=[self.algorithm_choose, self.choose_profile])
            widgets_list.append(widget)
            widget.algorithm.execution_done_extend.connect(self.execution_done)
            # widget.algorithm.progress_signal.connect(self.progress_info)
            self.stack_layout.addWidget(widget)

        self.label = QLabel()
        self.label.setWordWrap(True)
        layout = QVBoxLayout()
        layout2 = QHBoxLayout()
        layout2.setSpacing(1)
        layout2.setContentsMargins(0, 0, 0, 0)
        layout3 = QHBoxLayout()
        layout3.setContentsMargins(0, 0, 0, 0)
        layout.setContentsMargins(0, 0, 0, 0)
        layout4 = QHBoxLayout()
        layout4.setContentsMargins(0, 0, 0, 0)
        layout4.addWidget(self.save_profile_btn)
        layout4.addWidget(self.choose_profile)
        layout3.addWidget(self.interactive_use)
        layout3.addWidget(self.execute_btn)
        layout.addLayout(layout4)
        layout.addLayout(layout3)
        layout.addWidget(self.algorithm_choose)
        layout.addLayout(self.stack_layout)
        layout.addWidget(self.label)
        layout.addStretch(1)
        layout2.addWidget(self.off_left)
        layout2.addWidget(self.synchronize_checkbox)
        layout.addLayout(layout2)
        layout.addWidget(self._ch_control2)
        layout.setSpacing(0)
        self.setLayout(layout)
        self.algorithm_choose.currentIndexChanged.connect(self.stack_layout.setCurrentIndex)
        self.algorithm_choose.currentTextChanged.connect(self.algorithm_change)
        current_algorithm = self._settings.get("current_algorithm", self.algorithm_choose.currentText())
        for i, el in enumerate(widgets_list):
            if el.name == current_algorithm:
                self.algorithm_choose.setCurrentIndex(i)
                break

    def update_tooltips(self):
        for i in range(1, self.choose_profile.count()):
            if self.choose_profile.itemData(i, Qt.ToolTipRole) is not None:
                continue
            text = self.choose_profile.itemText(i)
            profile = self._settings.get(f"segmentation_profiles.{text}")
            tool_tip_text = profile["algorithm"] + "\n" + "\n".join(
                [f"{k.replace('_', ' ')}: {v}" for k, v in profile["values"].items()])
            self.choose_profile.setItemData(i, tool_tip_text, Qt.ToolTipRole)

    def event(self, event: QEvent):
        if event.type() == QEvent.WindowActivate:
            current_names = set(self._settings.get(f"segmentation_profiles", dict()).keys())
            prev_names = set([self.choose_profile.itemText(i) for i in range(1, self.choose_profile.count())])
            new_names = current_names - prev_names
            delete_names = prev_names - current_names
            if len(delete_names) > 0:
                i = 1
                while i < self.choose_profile.count():
                    if self.choose_profile.itemText(i) in delete_names:
                        self.choose_profile.removeItem(i)
                    else:
                        i += 1
            if len(new_names) > 0:
                self.choose_profile.addItems(list(sorted(new_names)))
            self.update_tooltips()
            algorithm_name =  self._settings.get("current_algorithm", self.algorithm_choose.currentText())
            if algorithm_name != self.algorithm_choose.currentText():
                interactive = self.interactive_use.isChecked()
                self.interactive_use.setChecked(False)
                try:
                    self._change_profile(algorithm_name, self._settings.get(f"algorithms.{algorithm_name}"))
                except KeyError:
                    pass
                self.interactive_use.setChecked(interactive)
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent):
        if (event.key() == Qt.Key_Enter or event.key() == Qt.Key_Return) and (event.modifiers() == Qt.ControlModifier):
            self.execute_btn.click()

    def save_profile(self):
        widget: InteractiveAlgorithmSettingsWidget = self.stack_layout.currentWidget()
        while True:
            text, ok = QInputDialog.getText(self, "Profile Name", "Input profile_name_here")
            if not ok:
                return
            if text in self._settings.get("segmentation_profiles", dict()):
                if QMessageBox.No == QMessageBox.warning(
                        self, "Already exists",
                        "Profile with this name already exist. Overwrite?",
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No):
                    continue
            print(widget.get_values(), text, ok)
            resp = {"algorithm": widget.name,
                    "values": widget.get_values()
                    }

            self._settings.set(f"segmentation_profiles.{text}", resp)
            self._settings.dump()
            self.choose_profile.addItem(text)
            self.update_tooltips()
            break

    def change_profile(self, val):
        self.choose_profile.setToolTip("")
        if val == "<none>":
            return
        interactive = self.interactive_use.isChecked()
        self.interactive_use.setChecked(False)
        profile = self._settings.get(f"segmentation_profiles.{val}")
        self._change_profile(profile["algorithm"], profile["values"])
        self.choose_profile.blockSignals(True)
        self.choose_profile.setCurrentIndex(0)
        self.choose_profile.blockSignals(False)
        self.interactive_use.setChecked(interactive)

    def _change_profile(self, name, values):
        for i in range(self.stack_layout.count()):
            widget: InteractiveAlgorithmSettingsWidget = self.stack_layout.widget(i)
            if widget.name == name:
                self.algorithm_choose.setCurrentIndex(i)
                widget.set_values(values)
                break
    @property
    def segmentation(self):
        return self._settings.segmentation

    @segmentation.setter
    def segmentation(self, val):
        self._settings.segmentation = val

    def image_changed(self):
        self.segmentation = None

    @property
    def interactive(self):
        return self.interactive_use.isChecked()

    def hide_left_panel(self, val):
        self.synchronize_checkbox.setChecked(not val)
        self.synchronize_checkbox.setDisabled(val)
        self.left_panel.parent().setHidden(val)

    def interactive_change(self, val):
        if val:
            self.execute_algorithm()

    def algorithm_change(self, val):
        self._settings.set("current_algorithm", val)
        if self.interactive:
            self.execute_algorithm()

    def image_changed_exec(self):
        if self.interactive:
            self.execute_algorithm()

    def execute_algorithm(self):
        widget: InteractiveAlgorithmSettingsWidget = self.stack_layout.currentWidget()
        self._settings.set("last_executed_algorithm", widget.name)
        widget.execute()

    def execution_done(self, segmentation, full_segmentation):
        self.segmentation = segmentation
        self._settings.full_segmentation = full_segmentation
        self.label.setText(", ".join(map(str, self._settings.sizes[1:])))


class MainMenu(QWidget):
    def __init__(self, settings: PartSettings):
        super().__init__()
        self._settings = settings
        self.open_btn = QPushButton("Open")
        self.save_btn = QPushButton("Save")
        self.advanced_btn = QPushButton("Advanced")
        self.interpolate_btn = QPushButton("Interpolate")
        self.mask_manager_btn = QPushButton("Mask Manager")
        self.batch_processing = QPushButton("Batch Processing")

        self.advanced_window = None

        layout = QHBoxLayout()
        layout.addWidget(self.open_btn)
        layout.addWidget(self.save_btn)
        layout.addWidget(self.advanced_btn)
        layout.addWidget(self.interpolate_btn)
        layout.addWidget(self.mask_manager_btn)
        layout.addWidget(self.batch_processing)
        self.setLayout(layout)

        self.open_btn.clicked.connect(self.load_data)
        self.advanced_btn.clicked.connect(self.advanced_window_show)
        self.mask_manager_btn.clicked.connect(self.mask_manager)
        self.interpolate_btn.clicked.connect(self.interpolate_exec)

    def interpolate_exec(self):
        dialog = InterpolateDialog(self._settings.image_spacing)
        if dialog.exec():
            scale_factor = dialog.get_zoom_factor()
            print(scale_factor)
            interp_ob = InterpolateThread()
            dial = WaitingDialog(interp_ob)
            args = [self._settings.image]
            if self._settings.mask is not None:
                mask = self._settings.mask.astype(np.uint8)
                mask[mask > 0] = 255
                args.append(mask)
            interp_ob.set_arrays(args)
            interp_ob.set_scaling(scale_factor)
            if dial.exec():
                self._settings.image = interp_ob.result[0], self._settings.image_path
                if len(interp_ob.result) == 2:
                    self._settings.mask = interp_ob.result[1] > 128
                self._settings.image_spacing = [x/y for x,y in zip(self._settings.image_spacing, scale_factor[::-1])]
            else:
                if interp_ob.isRunning():
                    interp_ob.terminate()
            #self.settings.rescale_image(dialog.get_zoom_factor())

    def mask_manager(self):
        if self._settings.segmentation is None:
            QMessageBox.information(self, "No segmentation", "Cannot create mask without segmentation")
            return
        dial = MaskWindow(self._settings)
        dial.exec_()

    def load_data(self):
        try:
            dial = QFileDialog(self, "Load data")
            dial.setDirectory(self._settings.get("io.open_directory", ""))
            dial.setFileMode(QFileDialog.ExistingFile)
            filters = ["raw image (*.tiff *.tif *.lsm)", "image with mask (*.tiff *.tif *.lsm)",
                       "mask to image (*.tiff *.tif *.lsm)", "image with current mask (*.tiff *.tif *.lsm)",
                       "saved project (*.tgz *.tbz2 *.gz *.bz2)", "Profiles (*.json)"]
            # dial.setFilters(filters)
            dial.setNameFilters(filters)
            dial.selectNameFilter(self._settings.get("io.open_filter", filters[0]))
            if dial.exec_():
                file_path = str(dial.selectedFiles()[0])
                self._settings.set("io.open_directory", os.path.dirname(str(file_path)))
                selected_filter = str(dial.selectedNameFilter())
                self._settings.set("io.open_filter", selected_filter)
                logging.debug("open file: {}, filter {}".format(file_path, selected_filter))
                # TODO maybe something better. Now main window have to be parent
                if selected_filter == "raw image (*.tiff *.tif *.lsm)":
                    im = tif.imread(file_path)
                    self._settings.image = im, file_path
                    #self._settings.image_spacing = list(np.array([70, 70 ,210]) * 0.1**9)
                elif selected_filter == "mask to image (*.tiff *.tif *.lsm)":
                    im = tif.imread(file_path)
                    self._settings.mask = im
                elif selected_filter == "image with current mask (*.tiff *.tif *.lsm)":
                    mask = self._settings
                    im = tif.imread(file_path)
                    self._settings.image = im, file_path
                    self._settings.mask = mask
                elif selected_filter == "image with mask (*.tiff *.tif *.lsm)":
                    extension = os.path.splitext(file_path)
                    if extension == ".json":
                        with open(file_path) as ff:
                            info_dict = json.load(ff)
                        image = tif.imread(info_dict["image"])
                        mask = tif.imread(info_dict["mask"])
                        self._settings.image = image, info_dict["image"]
                        self._settings.mask = mask
                    else:
                        image = tif.imread(file_path)
                        org_name = os.path.basename(file_path)
                        mask_dial = QFileDialog(self, "Load mask for {}".format(org_name))
                        filters = ["mask (*.tiff *.tif *.lsm)"]
                        mask_dial.setNameFilters(filters)
                        if mask_dial.exec_():
                            mask = tif.imread(mask_dial.selectedFiles()[0])
                            self._settings.image = image, file_path
                            self._settings.mask = mask
                elif selected_filter == "saved project (*.tgz *.tbz2 *.gz *.bz2)":
                    load_project(file_path, self._settings)
                    # self.segment.threshold_updated()
                elif selected_filter == "Profiles (*.json)":
                    self._settings.load_profiles(file_path)
                else:
                    # noinspection PyCallByClass
                    _ = QMessageBox.warning(self, "Load error", "Function do not implemented yet")
                    return
        except Exception as e:
            import traceback
            traceback.print_exc()
            QMessageBox.warning(self, "Open error", "Exception occurred {}".format(e))

    def save_file(self):
        try:
            dial = QFileDialog(self, "Save data")
            dial.setDirectory(self._settings.get("io.save_directory", self._settings.get("io.open_directory", "")))
            dial.setFileMode(QFileDialog.AnyFile)
            filters = ["Project (*.tgz *.tbz2 *.gz *.bz2)", "Labeled image (*.tif)", "Mask in tiff (*.tif)",
                       "Mask for itk-snap (*.img)", "Data for chimera (*.cmap)", "Image (*.tiff)", "Profiles (*.json)",
                       "Segmented data in xyz (*.xyz)"]
            dial.setAcceptMode(QFileDialog.AcceptSave)
            dial.setNameFilters(filters)
            default_name = os.path.splitext(os.path.basename(self._settings.file_path))[0]
            dial.selectFile(default_name)
            dial.selectNameFilter(self._settings.get("io.save_filter", ""))
            if dial.exec_():
                file_path = str(dial.selectedFiles()[0])
                selected_filter = str(dial.selectedNameFilter())
                self._settings.set("io.save_filter", selected_filter)
                self._settings.set("io.save_directory", os.path.dirname(file_path))
                if os.path.splitext(file_path)[1] == '':
                    ext = re.search(r'\(\*(\.\w+)', selected_filter).group(1)
                    file_path += ext
                    if os.path.exists(file_path):
                        # noinspection PyCallByClass
                        ret = QMessageBox.warning(self, "File exist", os.path.basename(file_path) +
                                                  " already exists.\nDo you want to replace it?",
                                                  QMessageBox.No | QMessageBox.Yes)
                        if ret == QMessageBox.No:
                            self.save_file()
                            return

                if selected_filter == "Project (*.tgz *.tbz2 *.gz *.bz2)":
                    save_project(file_path, self.settings, self.segment)

                elif selected_filter == "Labeled image (*.tif)":
                    save_labeled_image(file_path, self._settings)

                elif selected_filter == "Mask in tiff (*.tif)":
                    segmentation = self._settings.segmentation
                    segmentation = np.array(segmentation > 0).astype(np.uint8)
                    tif.imsave(file_path, segmentation)
                elif selected_filter == "Mask for itk-snap (*.img)":
                    segmentation = sitk.GetImageFromArray(self.segment.get_segmentation())
                    sitk.WriteImage(segmentation, file_path)
                elif selected_filter == "Data for chimera (*.cmap)":
                    if not np.any(self.segment.get_segmentation()):
                        QMessageBox.warning(self, "No object", "There is no component to export to cmap")
                        return
                    ob = CmapSave(file_path, self.settings, self.segment)
                    ob.exec_()
                elif selected_filter == "Image (*.tiff)":
                    image = self.settings.image
                    tif.imsave(file_path, image)
                elif selected_filter == "Profiles (*.json)":
                    self.settings.dump_profiles(file_path)
                elif selected_filter == "Segmented data in xyz (*.xyz)":
                    save_to_xyz(file_path, self.settings, self.segment)
                else:
                    # noinspection PyCallByClass
                    _ = QMessageBox.critical(self, "Save error", "Option unknown")
        except Exception as e:
            import traceback
            traceback.print_exc()
            QMessageBox.warning(self, "Open error", "Exception occurred {}".format(e))

    def advanced_window_show(self):
        if self.advanced_window is not None and self.advanced_window.isVisible():
            self.advanced_window.activateWindow()
            return
        self.advanced_window = AdvancedWindow(self._settings)
        self.advanced_window.show()


class MainWindow(QMainWindow):
    def __init__(self, title):
        super(MainWindow, self).__init__()
        self.setWindowTitle(title)
        self.title = title
        self.setMinimumWidth(600)
        self.settings = PartSettings(os.path.join(config_folder, "settings.json"))
        if os.path.exists(os.path.join(config_folder, "settings.json")):
            self.settings.load()
        self.main_menu = MainMenu(self.settings)
        # self.channel_control1 = ChannelControl(self.settings, name="raw_control", text="Left panel:")
        self.channel_control2 = ChannelControl(self.settings, name="result_control")
        self.raw_image = RawImageStack(self.settings,
                                       self.channel_control2)  # RawImageView(self.settings, self.channel_control1)
        self.result_image = ResultImageView(self.settings, self.channel_control2)
        self.color_bar = ColorBar(self.settings, self.raw_image.raw_image.channel_control)
        self.info_text = QLabel()
        self.raw_image.raw_image.text_info_change.connect(self.info_text.setText)
        self.result_image.text_info_change.connect(self.info_text.setText)
        self.synchronize_tool = SynchronizeView(self.raw_image.raw_image, self.result_image, self)
        # image_view_control = self.image_view.get_control_view()
        self.options_panel = Options(self.settings, self.channel_control2, self.raw_image.raw_image,
                                     self.synchronize_tool)
        # self.main_menu.image_loaded.connect(self.image_read)
        self.settings.image_changed.connect(self.image_read)

        im = tif.imread(os.path.join(static_file_folder, 'initial_images', "clean_segment.tiff"))
        self.settings.image = im

        icon = QIcon(os.path.join(static_file_folder, 'icons', "icon.png"))
        self.setWindowIcon(icon)

        layout = QGridLayout()
        layout.setSpacing(0)
        layout.addWidget(self.main_menu, 0, 0, 1, 3)
        layout.addWidget(self.info_text, 1, 0, 1, 3, Qt.AlignHCenter)  # , 0, 4)
        layout.addWidget(self.color_bar, 2, 0)
        layout.addWidget(self.raw_image, 2, 1)  # , 0, 0)
        layout.addWidget(self.result_image, 2, 2)  # , 0, 0)
        layout.addWidget(self.options_panel, 0, 3, 3, 1)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(2, 1)
        widget = QWidget()
        widget.setLayout(layout)
        self.setCentralWidget(widget)
        try:
            geometry = self.settings.get_from_profile("main_window_geometry")
            self.restoreGeometry(QByteArray.fromHex(bytes(geometry, 'ascii')))
        except KeyError:
            pass

    def image_read(self):
        self.raw_image.raw_image.set_image()
        self.raw_image.raw_image.reset_image_size()
        self.result_image.set_image()
        self.result_image.reset_image_size()
        self.options_panel.image_changed_exec()
        self.setWindowTitle(f"PartSeg: {self.settings.image_path}")

    def closeEvent(self, _):
        # print(self.settings.dump_view_profiles())
        # print(self.settings.segmentation_dict["default"].my_dict)
        self.settings.set_in_profile("main_window_geometry", bytes(self.saveGeometry().toHex()).decode('ascii'))
        self.settings.dump()


class MaskWindow(QDialog):
    """
    :type settings: Settings
    """
    def __init__(self, settings:PartSettings):
        """

        :type settings: Settings
        """
        super(MaskWindow, self).__init__()
        self.setWindowTitle("Mask manager")
        self.settings = settings
        main_layout = QVBoxLayout()
        dilate_label = QLabel("Dilate (x,y) radius (in pixels)", self)
        self.dilate_radius = QSpinBox(self)
        self.dilate_radius.setRange(-100, 100)
        self.dilate_radius.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.dilate_radius.setValue(self.settings.get("mask_manager.dilate_radius", 1))
        self.dilate_radius.setSingleStep(1)
        dilate_layout = QHBoxLayout()
        dilate_layout.addWidget(dilate_label)
        dilate_layout.addWidget(self.dilate_radius)
        main_layout.addLayout(dilate_layout)
        op_layout = QHBoxLayout()
        if len(settings.undo_segmentation_history) == 0:
            self.save_draw = QCheckBox("Save draw", self)
        else:
            self.save_draw = QCheckBox("Add draw", self)
        op_layout.addWidget(self.save_draw)
        self.fill_holes = QCheckBox("Fill holes", self)
        self.fill_holes.setToolTip("Fill holes that are not connected in 3d")
        op_layout.addWidget(self.fill_holes)
        self.fill_holes_in_2d = QCheckBox("Fill holes in 2d")
        self.fill_holes_in_2d.setToolTip("Fill holes thar are not connected to border on each slice separately")
        op_layout.addWidget(self.fill_holes_in_2d)
        self.reset_next = QPushButton("Reset Next")
        self.reset_next.clicked.connect(self.reset_next_fun)
        if len(settings.undo_segmentation_history) == 0:
            self.reset_next.setDisabled(True)
        op_layout.addStretch()
        op_layout.addWidget(self.reset_next)
        main_layout.addLayout(op_layout)
        self.prev_button = QPushButton(f"Previous mask ({len(settings.segmentation_history)})", self)
        if len(settings.segmentation_history) == 0:
            self.prev_button.setDisabled(True)
        self.cancel = QPushButton("Cancel", self)
        self.cancel.clicked.connect(self.close)
        self.next_button = QPushButton(f"Next mask ({len(settings.undo_segmentation_history)})", self)
        if len(settings.undo_segmentation_history) == 0:
            self.next_button.setText("Next mask (new)")
        self.next_button.clicked.connect(self.next_mask)
        self.prev_button.clicked.connect(self.prev_mask)
        button_layout = QHBoxLayout()
        button_layout.addWidget(self.prev_button)
        button_layout.addWidget(self.cancel)
        button_layout.addWidget(self.next_button)
        main_layout.addLayout(button_layout)
        self.setLayout(main_layout)

    def reset_next_fun(self):
        self.settings.undo_segmentation_settings = []
        self.next_button.setText("Next mask (new)")
        self.reset_next.setDisabled(True)

    def next_mask(self):
        dilate_radius = self.dilate_radius.value()
        self.settings.set("mask_manager.dilate_radius", dilate_radius)
        algorithm_name = self.settings.get("last_executed_algorithm")
        algorithm_values = self.settings.get(f"algorithms.{algorithm_name}")
        segmentation = self.settings.segmentation
        mask = segmentation > 0

        if dilate_radius > 0:
            mask = dilate(mask, dilate_radius)
        elif dilate_radius < 0:
            mask = erode(mask, -dilate_radius)
        mask = mask.astype(np.bool)
        compressed_segmentation = BytesIO()
        np.savez(compressed_segmentation, segmentation)
        if self.settings.mask is not None:
            compressed_mask = BytesIO()
            np.savez(compressed_mask, mask)
        else:
            compressed_mask = None
        self.settings.segmentation_history.append(
            HistoryElement(algorithm_name, algorithm_values, segmentation, compressed_mask))
        self.settings.undo_segmentation_history = []
        self.settings.mask = mask
        self.close()

    def prev_mask(self):
        history: HistoryElement = self.settings.segmentation_history.pop()
        self.settings.set("current_algorithm", history.algorithm_name)
        self.settings.set(f"algorithm.{history.algorithm_name}", history.algorithm_values)
        self.settings.segmentation = np.load(history.segmentation)
        if history.mask is not None:
            self.settings.mask = np.load(history.mask)
        else:
            self.settings.mask = None
        self.settings.change_segmentation_mask(self.segment, MaskChange.prev_seg, False)
        self.settings_updated_function()
        self.close()
