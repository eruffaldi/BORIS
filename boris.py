#!/usr/bin/env python3

"""
BORIS
Behavioral Observation Research Interactive Software
Copyright 2012-2017 Olivier Friard

This file is part of BORIS.

  BORIS is free software; you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation; either version 3 of the License, or
  any later version.

  BORIS is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU General Public License for more details.

  You should have received a copy of the GNU General Public License
  along with this program; if not see <http://www.gnu.org/licenses/>.

"""

import os
import sys
import platform
import logging
from optparse import OptionParser
import time
import json
from decimal import *
import re
import hashlib
import subprocess
import sqlite3
import urllib.parse
import urllib.request
import urllib.error
import tempfile
import glob
import statistics
import datetime
import multiprocessing
import socket

__version__ = "3.60"
__version_date__ = "2017-03-24"
__DEV__ = False


#BITMAP_EXT = "jpg"

if sys.platform == "darwin":  # for MacOS
    os.environ["LC_ALL"] = "en_US.UTF-8"

# check if argument
usage = "usage: %prog [options]"
parser = OptionParser(usage=usage)

parser.add_option("-d", "--debug", action="store_true", default=False, dest="debug", help="Verbose mode for debugging")
parser.add_option("-v", "--version", action="store_true", default=False, dest="version", help="Print version")
parser.add_option("-n", "--nosplashscreen", action="store_true", default=False, help="No splash screen")

(options, args) = parser.parse_args()

if options.version:
    print("version {0} release date: {1}".format(__version__, __version_date__))
    sys.exit(0)

# set logging parameters
if options.debug:
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.INFO)

if platform.python_version() < "3.4":
    logging.critical("BORIS requires Python 3.4+! You are using v. {}")
    sys.exit()

try:
    from PyQt5.QtCore import *
    from PyQt5.QtGui import *
    from PyQt5.QtWidgets import *
    from boris_ui5 import *
except:
    logging.info("PyQt5 not installed!\nTrying with PyQt4")
    try:
        from PyQt4.QtCore import *
        from PyQt4.QtGui import *
        from boris_ui import *

    except:
        logging.critical("PyQt4 not installed!\nTry PyQt4")
        sys.exit()

import qrc_boris

video, live = 0, 1

try:
    import matplotlib
    FLAG_MATPLOTLIB_INSTALLED = True
except:
    logging.warning("matplotlib plotting library not installed")
    FLAG_MATPLOTLIB_INSTALLED = False

FLAG_MATPLOTLIB_INSTALLED = True

import dialog
from edit_event import *
from project import *
import preferences
import param_panel
import observation
import modifiers_coding_map
import map_creator
import select_modifiers
from utilities import *
import tablib
import observations_list
import plot_spectrogram
import coding_pad
import transitions
import recode_widget

from config import *

def ffmpeg_recode(video_paths, horiz_resol, ffmpeg_bin):
    """
    recode one or more video with ffmpeg
    video_paths: list of video paths
    horiz_resol: horizontal resolution (in pixels)
    ffmpeg_bin: path of ffmpeg program
    """

    for video_path in video_paths:
        ffmpeg_command = ('"{ffmpeg_bin}" -y -i "{input_}" '
                          '-vf scale={horiz_resol}:-1 -b 2000k '
                          '"{input_}.re-encoded.{horiz_resol}px.avi" ').format(ffmpeg_bin=ffmpeg_bin,
                                                                              input_=video_path,
                                                                              horiz_resol=horiz_resol)
        p = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
        p.communicate()

    return True

def bytes_to_str(b):
    """
    Translate bytes to string.
    """
    if isinstance(b, bytes):
        fileSystemEncoding = sys.getfilesystemencoding()
        # hack for PyInstaller
        if fileSystemEncoding is None:
            fileSystemEncoding = "UTF-8"
        return b.decode(fileSystemEncoding)
    else:
        return b

from time_budget_widget import timeBudgetResults
import select_modifiers

class ProjectServerThread(QThread):
    """
    thread for serving project to BORIS mobile app
    """

    signal = pyqtSignal(dict)

    def __init__(self, message):
        QThread.__init__(self)
        self.message = message

    def __del__(self):
        self.wait()

    def run(self):

        BUFFER_SIZE = 1024

        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.settimeout(60)

        s.bind((get_ip_address(), 0))
        self.signal.emit({"URL": "{}:{}".format(s.getsockname()[0], s.getsockname()[1])})

        s.listen(5)
        while True:
            try:
                c, addr = s.accept()
                logging.info("Got connection from {}".format(addr))
            except socket.timeout:
                s.close()
                logging.info("Time out")
                self.signal.emit({"MESSAGE": "Server time out"})
                return

            rq = c.recv(BUFFER_SIZE)
            logging.info("request: {}".format(rq))

            if rq == b"get":
                while self.message:
                    c.send(self.message[0:BUFFER_SIZE])
                    self.message = self.message[BUFFER_SIZE:]
                c.close()
                logging.info("Project sent")
                self.signal.emit({"MESSAGE": "Project sent to {}".format(addr[0])})
                return

            if rq == b"stop":
                c.close()
                logging.info("server stopped")
                self.signal.emit({"MESSAGE": "The server is now stopped"})
                return

            # receive an observation
            if rq == b"put":
                print("put")
                c.send(b"SEND")
                print("sent SEND")
                c.close()

                print("listening")

                c2, addr = s.accept()
                print("accepted")

                rq2 = b""
                while 1:
                    d = c2.recv(BUFFER_SIZE)
                    if d:
                        rq2 += d
                        if rq2.endswith(b"#####"):
                            break
                    else:
                        break
                print("received", rq2)
                c2.close()
                self.signal.emit({"RECEIVED": "{}".format(rq2.decode("utf-8")), "SENDER": addr})
                return



class TempDirCleanerThread(QThread):
    """
    class for cleaning image cache directory with qthread
    """
    def __init__(self, parent = None):
        QThread.__init__(self, parent)
        self.exiting = False
        self.tempdir = ""
        self.ffmpeg_cache_dir_max_size = 0

    def run(self):
        while self.exiting == False:
            if sum(os.path.getsize(self.tempdir + f) for f in os.listdir(self.tempdir) if "BORIS@" in f and os.path.isfile(self.tempdir + f)) > self.ffmpeg_cache_dir_max_size:
                fl = sorted((os.path.getctime(self.tempdir + f), self.tempdir + f) for f in os.listdir(self.tempdir) if "BORIS@" in f and os.path.isfile(self.tempdir + f))
                for ts, f in fl[0:int(len(fl) / 10)]:
                    os.remove(f)
            time.sleep(30)
            logging.debug("cleaning frame cache directory")

ROW = -1 # red triangle


class StyledItemDelegateTriangle(QStyledItemDelegate):
    """
    painter for twEvents with current time highlighting
    """
    def __init__(self, parent=None):
        super(StyledItemDelegateTriangle, self).__init__(parent)

    def paint(self, painter, option, index):

        super(StyledItemDelegateTriangle, self).paint(painter, option, index)

        if ROW != -1:

            if index.row() == ROW:

                polygonTriangle = QPolygon(3)
                polygonTriangle.setPoint(0, QtCore.QPoint(option.rect.x() + 15, option.rect.y()))
                polygonTriangle.setPoint(1, QtCore.QPoint(option.rect.x(), option.rect.y() - 5))
                polygonTriangle.setPoint(2, QtCore.QPoint(option.rect.x(), option.rect.y() + 5))

                painter.save()
                painter.setRenderHint(painter.Antialiasing)
                painter.setBrush(QBrush(QColor(QtCore.Qt.red)))
                painter.setPen(QPen(QColor(QtCore.Qt.red)))
                painter.drawPolygon(polygonTriangle)
                painter.restore()


class MainWindow(QMainWindow, Ui_MainWindow):

    pj = {"time_format": HHMMSS,
          "project_date": "",
          "project_name": "",
          "project_description": "",
          SUBJECTS : {},
          ETHOGRAM: {},
          OBSERVATIONS: {} ,
          "coding_map":{} }
    project = False

    ffmpeg_recode_process = None

    observationId = ''   # current observation id

    timeOffset = 0.0

    confirmSound = False               # if True each keypress will be confirmed by a beep
    embedPlayer = True                 # if True the VLC player will be embedded in the main window
    detachFrameViewer = False          # if True frame are displayed in a separate window (frameViewer class in dialog)

    spectrogramHeight = 80
    spectrogram_color_map = SPECTROGRAM_DEFAULT_COLOR_MAP

    frame_bitmap_format = FRAME_DEFAULT_BITMAT_FORMAT

    alertNoFocalSubject = False        # if True an alert will show up if no focal subject
    trackingCursorAboveEvent = False   # if True the cursor will appear above the current event in events table
    checkForNewVersion = False         # if True BORIS will check for new version every 15 days
    timeFormat = HHMMSS                # 's' or 'hh:mm:ss'
    repositioningTimeOffset = 0
    automaticBackup = 0                # automatic backup interval (0 no backup)

    projectChanged = False

    liveObservationStarted = False

    frame_viewer1_mem_geometry = None
    frame_viewer2_mem_geometry = None

    projectFileName = ""
    mediaTotalLength = None

    saveMediaFilePath = True

    beep_every = 0

    measurement_w = None
    memPoints = []   # memory of clicked points for measurement tool

    behaviouralStringsSeparator = "|"

    duration = []
    duration2 = []

    simultaneousMedia = False  # if second player was created

    # time laps
    fast = 10

    currentStates = {}
    flag_slow = False
    play_rate = 1

    play_rate_step = 0.1

    currentSubject = ''  # contains the current subject of observation

    detailedObs = {}

    codingMapWindowGeometry = 0

    projectWindowGeometry = 0   # memorize size of project window

    imageDirectory = ""   # image cache directory

    # FFmpeg
    allowFrameByFrame = False

    memx, memy = -1, -1

    # path for ffmpeg/ffmpeg.exe program
    ffmpeg_bin = ''
    ffmpeg_cache_dir = ''
    ffmpeg_cache_dir_max_size = 0
    frame_resize = 0

    # dictionary for FPS storing
    fps, fps2 = {}, {}

    playerType = ""   # VLC, LIVE, VIEWER
    playMode = VLC    # player mode can be VLC of FMPEG (for frame-by-frame mode)

    # spectrogram
    chunk_length = 60  # spectrogram chunk length in seconds

    memMedia = ""

    close_the_same_current_event = False

    tcp_port = 0

    cleaningThread = TempDirCleanerThread()


    def __init__(self, availablePlayers, ffmpeg_bin, parent = None):

        super(MainWindow, self).__init__(parent)
        self.setupUi(self)

        self.availablePlayers = availablePlayers
        self.ffmpeg_bin = ffmpeg_bin
        # set icons
        self.setWindowIcon(QIcon(":/logo.png"))
        self.actionPlay.setIcon(QIcon(":/play.png"))
        self.actionPause.setIcon(QIcon(":/pause.png"))
        self.actionReset.setIcon(QIcon(":/reset.png"))
        self.actionJumpBackward.setIcon(QIcon(":/jump_backward.png"))
        self.actionJumpForward.setIcon(QIcon(":/jump_forward.png"))

        self.actionFaster.setIcon(QIcon(":/faster.png"))
        self.actionSlower.setIcon(QIcon(":/slower.png"))
        self.actionNormalSpeed.setIcon(QIcon(":/normal_speed.png"))

        self.actionPrevious.setIcon(QIcon(":/previous.png"))
        self.actionNext.setIcon(QIcon(":/next.png"))

        self.actionSnapshot.setIcon(QIcon(":/snapshot.png"))

        self.actionFrame_by_frame.setIcon(QIcon(":/frame_mode"))
        self.actionFrame_backward.setIcon(QIcon(":/frame_backward"))
        self.actionFrame_forward.setIcon(QIcon(":/frame_forward"))

        self.setWindowTitle("{} ({})".format(programName, __version__))

        '''
        try:
            datadir = sys._MEIPASS
        except Exception:
            datadir = os.path.dirname(os.path.realpath(__file__))
        '''

        if os.path.isfile(sys.path[0]):  # for pyinstaller
            datadir = os.path.dirname(sys.path[0])
        else:
            datadir = sys.path[0]

        self.lbLogoBoris.setPixmap(QPixmap(datadir + "/logo_boris_500px.png"))
        self.lbLogoBoris.setScaledContents(False)
        self.lbLogoBoris.setAlignment(Qt.AlignCenter)


        self.lbLogoUnito.setPixmap(QPixmap(datadir + "/dbios_unito.png"))
        self.lbLogoUnito.setScaledContents(False)
        self.lbLogoUnito.setAlignment(Qt.AlignCenter)


        self.toolBar.setEnabled(False)
        # remove default page from toolBox
        self.toolBox.removeItem(0)
        self.toolBox.setVisible(False)

        # start with dock widget invisible
        self.dwObservations.setVisible(False)
        self.dwEthogram.setVisible(False)
        self.dwSubjects.setVisible(False)
        self.lbFocalSubject.setVisible(False)
        self.lbCurrentStates.setVisible(False)

        self.lbFocalSubject.setText("")
        self.lbCurrentStates.setText("")

        self.lbFocalSubject.setText(NO_FOCAL_SUBJECT)

        font = QFont()
        font.setPointSize(15)
        self.lbFocalSubject.setFont(font)
        self.lbCurrentStates.setFont(font)

        # add label to status bar
        self.lbTime = QLabel()
        self.lbTime.setFrameStyle(QFrame.StyledPanel)
        self.lbTime.setMinimumWidth(160)
        self.statusbar.addPermanentWidget(self.lbTime)

        # current subjects
        self.lbSubject = QLabel()
        self.lbSubject.setFrameStyle(QFrame.StyledPanel)
        self.lbSubject.setMinimumWidth(160)
        self.statusbar.addPermanentWidget(self.lbSubject)


        # time offset
        self.lbTimeOffset = QLabel()
        self.lbTimeOffset.setFrameStyle(QFrame.StyledPanel)
        self.lbTimeOffset.setMinimumWidth(160)
        self.statusbar.addPermanentWidget(self.lbTimeOffset)

        # speed
        self.lbSpeed = QLabel()
        self.lbSpeed.setFrameStyle(QFrame.StyledPanel)
        self.lbSpeed.setMinimumWidth(40)
        self.statusbar.addPermanentWidget(self.lbSpeed)

        # set painter for twEvents to highlight current row
        self.twEvents.setItemDelegate(StyledItemDelegateTriangle(self.twEvents))

        self.twEvents.setColumnCount( len(tw_events_fields) )
        self.twEvents.setHorizontalHeaderLabels(tw_events_fields)

        self.imagesList = set()
        self.FFmpegGlobalFrame = 0

        self.menu_options()

        self.connections()


    def create_live_tab(self):
        """
        create tab with widget for live observation
        """

        self.liveLayout = QGridLayout()
        self.textButton = QPushButton("Start live observation")
        self.textButton.clicked.connect(self.start_live_observation)
        self.liveLayout.addWidget(self.textButton)

        self.lbTimeLive = QLabel()
        self.lbTimeLive.setAlignment(Qt.AlignCenter)

        font = QFont("Monospace")
        font.setPointSize(48)
        self.lbTimeLive.setFont(font)
        if self.timeFormat == HHMMSS:
            self.lbTimeLive.setText("00:00:00.000")
        if self.timeFormat == S:
            self.lbTimeLive.setText("0.000")

        self.liveLayout.addWidget(self.lbTimeLive)

        self.liveTab = QWidget()
        self.liveTab.setLayout(self.liveLayout)

        self.toolBox.insertItem(2, self.liveTab, 'Live')


    def menu_options(self):
        """
        enable/disable menu option
        """
        logging.debug("menu_options function")

        flag = self.project

        if not self.project:
            pn = ""
        else:
            if self.pj["project_name"]:
                pn = self.pj["project_name"]
            else:
                if self.projectFileName:
                    pn = "Unnamed project ({})".format(self.projectFileName)
                else:
                    pn = "Unnamed project"

        self.setWindowTitle("{}{}{}".format(self.observationId + " - "*(self.observationId != ""), pn+(" - "*(pn != "")), programName))

        # project menu
        self.actionEdit_project.setEnabled(flag)
        self.actionSave_project.setEnabled(flag)
        self.actionSave_project_as.setEnabled(flag)
        self.actionClose_project.setEnabled(flag)

        self.actionSend_project.setEnabled(flag)
        # observations

        # enabled if project
        self.actionNew_observation.setEnabled(flag)

        self.actionOpen_observation.setEnabled(self.pj[OBSERVATIONS] != {})
        self.actionEdit_observation_2.setEnabled(self.pj[OBSERVATIONS] != {})
        self.actionObservationsList.setEnabled(self.pj[OBSERVATIONS] != {})

        # enabled if observation
        flagObs = self.observationId != ""

        self.actionAdd_event.setEnabled(flagObs)
        self.actionClose_observation.setEnabled(flagObs)
        self.actionLoad_observations_file.setEnabled(flag)

        '''self.menuExport_events.setEnabled(flag)'''
        self.actionExportEvents.setEnabled(flag)
        self.actionExport_aggregated_events.setEnabled(flag)

        self.actionExportEventString.setEnabled(flag)
        self.actionExport_events_as_Praat_TextGrid.setEnabled(flag)
        self.actionExtract_events_from_media_files.setEnabled(flag)

        self.actionDelete_all_observations.setEnabled(flagObs)
        self.actionSelect_observations.setEnabled(flagObs)
        self.actionDelete_selected_observations.setEnabled(flagObs)
        self.actionEdit_event.setEnabled(flagObs)
        self.actionEdit_selected_events.setEnabled(flagObs)
        self.actionFind_events.setEnabled(flagObs)
        self.actionFind_replace_events.setEnabled(flagObs)


        self.actionCheckStateEvents.setEnabled(flag)


        self.actionMedia_file_information.setEnabled(flagObs)
        self.actionMedia_file_information.setEnabled(self.playerType == VLC)
        self.menuCreate_subtitles_2.setEnabled(flag)

        self.actionJumpForward.setEnabled(self.playerType == VLC)
        self.actionJumpBackward.setEnabled(self.playerType == VLC)
        self.actionJumpTo.setEnabled(self.playerType == VLC)

        self.menuZoom1.setEnabled((self.playerType == VLC) and (self.playMode == VLC))
        self.menuZoom2.setEnabled(False)
        try:
            zv = self.mediaplayer.video_get_scale()
            self.actionZoom1_fitwindow.setChecked(zv == 0)
            self.actionZoom1_1_1.setChecked(zv == 1)
            self.actionZoom1_1_2.setChecked(zv == 0.5)
            self.actionZoom1_1_4.setChecked(zv == 0.25)
            self.actionZoom1_2_1.setChecked(zv == 2)
        except:
            pass

        if self.simultaneousMedia:
            self.menuZoom2.setEnabled((self.playerType == VLC) and (self.playMode == VLC))
            try:
                zv = self.mediaplayer2.video_get_scale()
                self.actionZoom2_fitwindow.setChecked(zv == 0)
                self.actionZoom2_1_1.setChecked(zv == 1)
                self.actionZoom2_1_2.setChecked(zv == 0.5)
                self.actionZoom2_1_4.setChecked(zv == 0.25)
                self.actionZoom2_2_1.setChecked(zv == 2)
            except:
                pass


        self.actionPlay.setEnabled(self.playerType == VLC)
        self.actionPause.setEnabled(self.playerType == VLC)
        self.actionReset.setEnabled(self.playerType == VLC)
        self.actionFaster.setEnabled(self.playerType == VLC)
        self.actionSlower.setEnabled(self.playerType == VLC)
        self.actionNormalSpeed.setEnabled(self.playerType == VLC)
        self.actionPrevious.setEnabled(self.playerType == VLC)
        self.actionNext.setEnabled(self.playerType == VLC)
        self.actionSnapshot.setEnabled(self.playerType == VLC)
        self.actionFrame_by_frame.setEnabled(True)

        self.actionFrame_backward.setEnabled(flagObs and (self.playMode == FFMPEG))
        self.actionFrame_forward.setEnabled(flagObs and (self.playMode == FFMPEG))

        # Tools
        if FLAG_MATPLOTLIB_INSTALLED:
            self.actionShow_spectrogram.setEnabled(flagObs)
        else:
            self.actionShow_spectrogram.setEnabled(False)
        # geometric measurements
        self.actionDistance.setEnabled(flagObs and (self.playMode == FFMPEG))
        self.actionBehaviors_map.setEnabled(flagObs)

        # Analysis
        self.actionTime_budget.setEnabled(self.pj[OBSERVATIONS] != {})
        self.actionTime_budget_by_behaviors_category.setEnabled(self.pj[OBSERVATIONS] != {})

        # plot events
        if FLAG_MATPLOTLIB_INSTALLED:
            self.actionVisualize_data.setEnabled(self.pj[OBSERVATIONS] != {})
        else:
            self.actionVisualize_data.setEnabled(False)

        self.menuCreate_transitions_matrix.setEnabled(self.pj[OBSERVATIONS] != {})

        # statusbar label
        self.lbTime.setVisible(self.playerType == VLC)
        self.lbSubject.setVisible(self.playerType == VLC)
        self.lbTimeOffset.setVisible(self.playerType == VLC)
        self.lbSpeed.setVisible(self.playerType == VLC)



    def connections(self):

        # menu file
        self.actionNew_project.triggered.connect(self.new_project_activated)
        self.actionOpen_project.triggered.connect(self.open_project_activated)
        self.actionEdit_project.triggered.connect(self.edit_project_activated)
        self.actionSave_project.triggered.connect(self.save_project_activated)
        self.actionSave_project_as.triggered.connect(self.save_project_as_activated)
        self.actionClose_project.triggered.connect(self.close_project)

        self.actionSend_project.triggered.connect(self.send_project_via_socket)

        self.menuCreate_subtitles_2.triggered.connect(self.create_subtitles)

        self.actionPreferences.triggered.connect(self.preferences)

        self.actionQuit.triggered.connect(self.actionQuit_activated)

        # menu observations
        self.actionNew_observation.triggered.connect(self.new_observation_triggered)
        self.actionOpen_observation.triggered.connect(self.open_observation)
        self.actionEdit_observation_2.triggered.connect(self.edit_observation )
        self.actionObservationsList.triggered.connect(self.observations_list)

        self.actionClose_observation.triggered.connect(self.close_observation)

        self.actionAdd_event.triggered.connect(self.add_event)
        self.actionEdit_event.triggered.connect(self.edit_event)

        self.actionCheckStateEvents.triggered.connect(self.check_state_events)

        self.actionSelect_observations.triggered.connect(self.select_events_between_activated)

        self.actionEdit_selected_events.triggered.connect(self.edit_selected_events)
        self.actionFind_events.triggered.connect(self.find_events)
        self.actionFind_replace_events.triggered.connect(self.find_replace_events)
        self.actionDelete_all_observations.triggered.connect(self.delete_all_events)
        self.actionDelete_selected_observations.triggered.connect(self.delete_selected_events)

        self.actionMedia_file_information.triggered.connect(self.media_file_info)

        self.actionLoad_observations_file.triggered.connect(self.import_observations)

        self.actionExportEvents.triggered.connect(self.export_tabular_events)


        self.actionExportEventString.triggered.connect(self.export_string_events)

        self.actionExport_aggregated_events.triggered.connect(self.export_aggregated_events)

        self.actionExport_events_as_Praat_TextGrid.triggered.connect(self.export_state_events_as_textgrid)

        self.actionExtract_events_from_media_files.triggered.connect(self.extract_events)

        self.actionAll_transitions.triggered.connect(lambda: self.transitions_matrix("frequency"))
        self.actionNumber_of_transitions.triggered.connect(lambda: self.transitions_matrix("number"))
        self.actionFrequencies_of_transitions_after_behaviors.triggered.connect(lambda: self.transitions_matrix("frequencies_after_behaviors"))


        # menu playback
        self.actionJumpTo.triggered.connect(self.jump_to)

        # menu Tools
        self.actionMapCreator.triggered.connect(self.map_creator)
        self.actionShow_spectrogram.triggered.connect(self.show_spectrogram)
        self.actionDistance.triggered.connect(self.distance)
        self.actionBehaviors_map.triggered.connect(self.show_coding_pad)
        self.actionRecode_resize_video.triggered.connect(self.recode_resize_video)
        self.actionMedia_file_information_2.triggered.connect(self.media_file_info)

        self.actionCreate_transitions_flow_diagram.triggered.connect(self.transitions_dot_script)
        self.actionCreate_transitions_flow_diagram_2.triggered.connect(self.transitions_flow_diagram)


        # menu Analyze
        #self.actionTime_budget.triggered.connect(self.time_budget)
        self.actionTime_budget.triggered.connect(lambda: self.time_budget_by_category("by_behavior"))
        self.actionTime_budget_by_behaviors_category.triggered.connect(lambda: self.time_budget_by_category("by_category"))

        self.actionVisualize_data.triggered.connect(self.plot_events)

        # menu Help
        self.actionUser_guide.triggered.connect(self.actionUser_guide_triggered)
        self.actionHow_to_cite_BORIS.triggered.connect(self.actionHow_to_cite_BORIS_activated)
        self.actionAbout.triggered.connect(self.actionAbout_activated)
        self.actionCheckUpdate.triggered.connect(self.actionCheckUpdate_activated)


        # toolbar
        self.actionPlay.triggered.connect(self.play_activated)
        self.actionPause.triggered.connect(self.pause_video)
        self.actionReset.triggered.connect(self.reset_activated)
        self.actionJumpBackward.triggered.connect(self.jumpBackward_activated)
        self.actionJumpForward.triggered.connect(self.jumpForward_activated)

        #self.actionAll_transitions.triggered.connect(lambda: self.transitions_matrix("frequency"))

        self.actionZoom1_fitwindow.triggered.connect(lambda: self.video_zoom(1, 0))
        self.actionZoom1_1_1.triggered.connect(lambda: self.video_zoom(1, 1))
        self.actionZoom1_1_2.triggered.connect(lambda: self.video_zoom(1, 0.5))
        self.actionZoom1_1_4.triggered.connect(lambda: self.video_zoom(1, 0.25))
        self.actionZoom1_2_1.triggered.connect(lambda: self.video_zoom(1, 2))

        self.actionZoom2_fitwindow.triggered.connect(lambda: self.video_zoom(2, 0))
        self.actionZoom2_1_1.triggered.connect(lambda: self.video_zoom(2, 1))
        self.actionZoom2_1_2.triggered.connect(lambda: self.video_zoom(2, 0.5))
        self.actionZoom2_1_4.triggered.connect(lambda: self.video_zoom(2, 0.25))
        self.actionZoom2_2_1.triggered.connect(lambda: self.video_zoom(2, 2))


        self.actionFaster.triggered.connect(self.video_faster_activated)
        self.actionSlower.triggered.connect(self.video_slower_activated)
        self.actionNormalSpeed.triggered.connect(self.video_normalspeed_activated)

        self.actionPrevious.triggered.connect(self.previous_media_file)
        self.actionNext.triggered.connect(self.next_media_file)

        self.actionSnapshot.triggered.connect(self.snapshot)

        self.actionFrame_by_frame.triggered.connect(self.switch_playing_mode)

        self.actionFrame_backward.triggered.connect(self.frame_backward)
        self.actionFrame_forward.triggered.connect(self.frame_forward)

        # table Widget double click
        self.twEvents.itemDoubleClicked.connect(self.twEvents_doubleClicked)
        self.twEthogram.itemDoubleClicked.connect(self.twEthogram_doubleClicked)
        self.twSubjects.itemDoubleClicked.connect(self.twSubjects_doubleClicked)

        # Actions for twEthogram context menu
        self.twEthogram.setContextMenuPolicy(Qt.ActionsContextMenu)
        self.actionFilterBehaviors.triggered.connect(self.filter_behaviors)
        self.twEthogram.addAction(self.actionFilterBehaviors)

        self.actionShowAllBehaviors.triggered.connect(self.show_all_behaviors)
        self.twEthogram.addAction(self.actionShowAllBehaviors)

        # Actions for twSubjects context menu
        self.twSubjects.setContextMenuPolicy(Qt.ActionsContextMenu)
        self.actionFilterSubjects.triggered.connect(self.filter_subjects)
        self.twSubjects.addAction(self.actionFilterSubjects)

        self.actionShowAllSubjects.triggered.connect(self.show_all_subjects)
        self.twSubjects.addAction(self.actionShowAllSubjects)


        # Actions for twEvents context menu
        self.twEvents.setContextMenuPolicy(Qt.ActionsContextMenu)

        self.twEvents.addAction(self.actionAdd_event)
        self.twEvents.addAction(self.actionEdit_selected_events)
        self.twEvents.addAction(self.actionFind_events)
        self.twEvents.addAction(self.actionFind_replace_events)

        separator2 = QAction(self)
        separator2.setSeparator(True)
        self.twEvents.addAction(separator2)

        self.twEvents.addAction(self.actionCheckStateEvents)

        separator2 = QAction(self)
        separator2.setSeparator(True)
        self.twEvents.addAction(separator2)

        self.twEvents.addAction(self.actionDelete_selected_observations)
        self.twEvents.addAction(self.actionDelete_all_observations)


        # Actions for twSubjects context menu
        self.actionDeselectCurrentSubject.triggered.connect(self.deselectSubject)

        self.twSubjects.setContextMenuPolicy(Qt.ActionsContextMenu)
        self.twSubjects.addAction(self.actionDeselectCurrentSubject)

        # subjects

        # timer for playing
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.timer_out)

        # timer for spectrogram visualization
        self.timer_spectro = QTimer(self)
        self.timer_spectro.setInterval(50)
        self.timer_spectro.timeout.connect(self.timer_spectro_out)

        # timer for timing the live observation
        self.liveTimer = QTimer(self)
        self.liveTimer.timeout.connect(self.liveTimer_out)

        self.readConfigFile()

        # timer for automatic backup
        self.automaticBackupTimer = QTimer(self)
        self.automaticBackupTimer.timeout.connect(self.automatic_backup)
        if self.automaticBackup:
            self.automaticBackupTimer.start(self.automaticBackup * 60000)


    def send_project_via_socket(self):
        """
        send project to a device via socket
        """

        def receive_signal(msg_dict):

            print(msg_dict)

            if "RECEIVED" in msg_dict:
                try:
                    sent_obs = json.loads(msg_dict["RECEIVED"][:-5]) # cut final #####
                except:
                    print("error")
                    del self.w
                    self.actionSend_project.setText("Project server")
                    return

                print("decoded", type(sent_obs), len(sent_obs))
                del self.w
                self.actionSend_project.setText("Project server")

                flag_msg = False
                mem_obsid = ""
                for obsId in sent_obs:
                    if obsId in self.pj[OBSERVATIONS]:
                        flag_msg = True
                        response = dialog.MessageDialog(programName, "The observation <b>{}</b> received from <b>{}</b><br>already exists in the current project.".format(obsId, msg_dict["SENDER"][0]),
                                                        ["Overwrite it", "Rename received observation", CANCEL])
                        if response == CANCEL:
                            return
                        if response == "Overwrite it":
                            self.pj[OBSERVATIONS][obsId] = dict(sent_obs[obsId])
                        if response == "Rename received observation":

                            new_id = obsId
                            while new_id in self.pj[OBSERVATIONS]:
                                new_id, ok = QInputDialog.getText(self, "Rename observation received from {}".format(msg_dict["SENDER"][0]),
                                                                    "New observation id:", QLineEdit.Normal, new_id)

                            self.pj[OBSERVATIONS][new_id] = dict(sent_obs[obsId])
                    else:
                        self.pj[OBSERVATIONS][obsId] = dict(sent_obs[obsId])
                        mem_obsid = obsId

                if not flag_msg:
                    QMessageBox.information(self, "Project server", "Observation {} received".format(mem_obsId))


            elif "URL" in msg_dict:
                self.tcp_port = int(msg_dict["URL"].split(":")[-1])
                self.w.label.setText("Project server URL:<br><b>{}</b><br><br>Time out: 60 seconds".format(msg_dict["URL"]))

            else:
                del self.w
                self.actionSend_project.setText("Project server")
                QMessageBox.information(self, "Project server", msg_dict["MESSAGE"])


        if "server" in self.actionSend_project.text():

            include_obs = dialog.MessageDialog(programName, "Include observations?", [YES, NO, CANCEL])
            if include_obs == CANCEL:
                return

            self.w = recode_widget.Info_widget()
            self.w.resize(450, 100)
            self.w.setWindowFlags(Qt.WindowStaysOnTopHint)
            self.w.setWindowTitle("Project server")
            self.w.label.setText("")
            self.w.show()
            app.processEvents()

            cp_project = dict(self.pj)
            if include_obs == NO:
                cp_project[OBSERVATIONS] = {}

            self.server_thread = ProjectServerThread(message=str.encode(str(json.dumps(cp_project,
                                                                            indent=None,
                                                                            separators=(",", ":"),
                                                                            default=decimal_default))))
            self.server_thread.signal.connect(receive_signal)

            self.server_thread.start()

            self.actionSend_project.setText("Stop serving project")

        # send stop msg to project server
        elif "serving" in self.actionSend_project.text():

            s = socket.socket()
            s.connect((get_ip_address(), self.tcp_port))
            s.send(str.encode("stop"))
            received = ""
            while 1:
                data = s.recv(20) # BUFFER_SIZE = 20
                if not data:
                    break
                received += data
            s.close


    def recode_resize_video(self):
        """
        re-encode video with ffmpeg
        """

        timerFFmpegRecoding = QTimer()

        def timerFFmpegRecoding_timeout():
            """
            check if process finished
            """
            if not self.ffmpeg_recode_process.is_alive():
                timerFFmpegRecoding.stop()
                self.w.hide()
                del(self.w)
                self.ffmpeg_recode_process = None

        if self.ffmpeg_recode_process:
            QMessageBox.warning(self, programName, "BORIS is already re-encoding a video...")
            return

        if QT_VERSION_STR[0] == "4":
            fileNames = QFileDialog(self).getOpenFileNames(self, "Select one or more media files to re-encode/resize", "", "Media files (*)")
        else:
            fileNames, _ = QFileDialog(self).getOpenFileNames(self, "Select one or more media files to re-encode/resize", "", "Media files (*)")

        if fileNames:

            horiz_resol, ok = QInputDialog.getInt(self, "", ("Horizontal resolution (in pixels)\n"
                                                             "The aspect ratio will be maintained"), 1024, 352, 1920, 10)

            # check if recoded files already exist
            files_list = []
            for file_name in fileNames:
                if os.path.isfile("{input}.re-encoded.{horiz_resol}px.avi".format(input=file_name, horiz_resol=horiz_resol)):
                    files_list.append("{input}.re-encoded.{horiz_resol}px.avi".format(input=file_name, horiz_resol=horiz_resol))
            if files_list:
                response = dialog.MessageDialog(programName, "Some file(s) already exist.\n\n" + "\n".join(files_list),
                     ["Overwrite all", CANCEL])
                if response == CANCEL:
                    return

            self.w = recode_widget.Info_widget()
            self.w.resize(350, 100)
            self.w.setWindowFlags(Qt.WindowStaysOnTopHint)
            self.w.setWindowTitle("Re-encoding and resizing with FFmpeg")
            self.w.label.setText("This operation can be long. Be patient...\n\n" + "\n".join(fileNames))
            self.w.show()

            if sys.platform.startswith("win") and getattr(sys, "frozen", False):
                app.processEvents()
                ffmpeg_recode(fileNames, horiz_resol, ffmpeg_bin)
                self.w.hide()
            else:

                self.ffmpeg_recode_process = multiprocessing.Process(target=ffmpeg_recode,
                                                                     args=(fileNames, horiz_resol, ffmpeg_bin, ))
                self.ffmpeg_recode_process.start()

                timerFFmpegRecoding.timeout.connect(timerFFmpegRecoding_timeout)
                timerFFmpegRecoding.start(15000)


    def click_signal_from_behaviors_map(self, behaviorCode):

        sendEventSignal = pyqtSignal(QEvent)

        sorted([self.pj[ETHOGRAM][x]["code"] for x in self.pj[ETHOGRAM]])

        #q = QKeyEvent()
        q = QKeyEvent(QEvent.KeyPress, Qt.Key_Enter, Qt.NoModifier, text=behaviorCode)

        self.keyPressEvent(q)


    def signal_from_behaviors_map(self, event):
        """
        receive signal from behaviors map
        """
        self.keyPressEvent(event)


    def show_coding_pad(self):
        """
        show coding pad window
        """
        if self.playerType == VIEWER:
            QMessageBox.warning(self, programName, "The coding pad is not available in <b>VIEW</b> mode")
            return

        if hasattr(self, "codingpad"):
            self.codingpad.show()
        else:
            self.codingpad = coding_pad.CodingPad(self.pj)
            self.codingpad.setWindowFlags(Qt.WindowStaysOnTopHint)
            self.codingpad.sendEventSignal.connect(self.signal_from_behaviors_map)
            self.codingpad.clickSignal.connect(self.click_signal_from_behaviors_map)
            self.codingpad.show()



    def show_all_behaviors(self):
        """
        show all behaviors in ethogram
        """
        self.load_behaviors_in_twEthogram([self.pj[ETHOGRAM][x]["code"] for x in self.pj[ETHOGRAM]])


    def show_all_subjects(self):
        """
        show all subjects in subjects list
        """
        self.load_subjects_in_twSubjects([self.pj[SUBJECTS][x]["name"] for x in self.pj[SUBJECTS]])



    def filter_behaviors(self):
        """
        allow user to filter behaviors in ethogram
        """

        paramPanelWindow = param_panel.Param_panel()
        paramPanelWindow.setWindowTitle("Select the behaviors to show in the ethogram table")
        paramPanelWindow.lwSubjects.setVisible(False)

        paramPanelWindow.pbSelectAllSubjects.setVisible(False)
        paramPanelWindow.pbUnselectAllSubjects.setVisible(False)
        paramPanelWindow.pbReverseSubjectsSelection.setVisible(False)

        paramPanelWindow.lbSubjects.setVisible(False)
        paramPanelWindow.cbIncludeModifiers.setVisible(False)
        paramPanelWindow.cbExcludeBehaviors.setVisible(False)
        paramPanelWindow.lbStartTime.setVisible(False)
        paramPanelWindow.teStartTime.setVisible(False)
        paramPanelWindow.dsbStartTime.setVisible(False)
        paramPanelWindow.lbEndTime.setVisible(False)
        paramPanelWindow.teEndTime.setVisible(False)
        paramPanelWindow.dsbEndTime.setVisible(False)

        # behaviors  filtered
        filtered_behaviors = [self.twEthogram.item(i, 1).text() for i in range(self.twEthogram.rowCount())]

        if BEHAVIORAL_CATEGORIES in self.pj:
            categories = self.pj[BEHAVIORAL_CATEGORIES][:]
            # check if behavior not included in a category
            if "" in [self.pj[ETHOGRAM][idx]["category"] for idx in self.pj[ETHOGRAM] if "category" in self.pj[ETHOGRAM][idx]]:
                categories += [""]
        else:
            categories = ["###no category###"]

        for category in categories:

            if category != "###no category###":

                if category == "":
                    paramPanelWindow.item = QListWidgetItem("No category")
                    paramPanelWindow.item.setData(34, "No category")
                else:
                    paramPanelWindow.item = QListWidgetItem(category)
                    paramPanelWindow.item.setData(34, category)

                font = QFont()
                font.setBold(True)
                paramPanelWindow.item.setFont(font)
                paramPanelWindow.item.setData(33, "category")
                paramPanelWindow.item.setData(35, False)

                paramPanelWindow.lwBehaviors.addItem(paramPanelWindow.item)

            for behavior in [self.pj[ETHOGRAM][x]["code"] for x in sorted_keys(self.pj[ETHOGRAM])]:

                if ((categories == ["###no category###"])
                or (behavior in [self.pj[ETHOGRAM][x]["code"] for x in self.pj[ETHOGRAM] if "category" in self.pj[ETHOGRAM][x] and self.pj[ETHOGRAM][x]["category"] == category])):

                    paramPanelWindow.item = QListWidgetItem(behavior)
                    if behavior in filtered_behaviors:
                        paramPanelWindow.item.setCheckState(Qt.Checked)
                    else:
                        paramPanelWindow.item.setCheckState(Qt.Unchecked)

                    if category != "###no category###":
                        paramPanelWindow.item.setData(33, "behavior")
                        if category == "":
                            paramPanelWindow.item.setData(34, "No category")
                        else:
                            paramPanelWindow.item.setData(34, category)

                    paramPanelWindow.lwBehaviors.addItem(paramPanelWindow.item)

        if paramPanelWindow.exec_():
            if self.observationId and set(paramPanelWindow.selectedBehaviors) != set(filtered_behaviors):
                self.projectChanged = True
            self.load_behaviors_in_twEthogram(paramPanelWindow.selectedBehaviors)


    def filter_subjects(self):
        paramPanelWindow = param_panel.Param_panel()
        paramPanelWindow.setWindowTitle("Select the subjects to show in the subjects list")
        paramPanelWindow.lwSubjects.setVisible(False)

        paramPanelWindow.pbSelectAllSubjects.setVisible(False)
        paramPanelWindow.pbUnselectAllSubjects.setVisible(False)
        paramPanelWindow.pbReverseSubjectsSelection.setVisible(False)

        paramPanelWindow.lbSubjects.setVisible(False)
        paramPanelWindow.cbIncludeModifiers.setVisible(False)
        paramPanelWindow.cbExcludeBehaviors.setVisible(False)
        paramPanelWindow.lbStartTime.setVisible(False)
        paramPanelWindow.teStartTime.setVisible(False)
        paramPanelWindow.dsbStartTime.setVisible(False)
        paramPanelWindow.lbEndTime.setVisible(False)
        paramPanelWindow.teEndTime.setVisible(False)
        paramPanelWindow.dsbEndTime.setVisible(False)

        # behaviors  filtered
        filtered_subjects = [self.twSubjects.item(i, 1).text() for i in range(self.twSubjects.rowCount())]

        for subject in [self.pj[SUBJECTS][x]["name"] for x in  sorted_keys(self.pj[SUBJECTS])]:

            paramPanelWindow.item = QListWidgetItem(subject)
            if subject in filtered_subjects:
                paramPanelWindow.item.setCheckState(Qt.Checked)
            else:
                paramPanelWindow.item.setCheckState(Qt.Unchecked)

            paramPanelWindow.lwBehaviors.addItem(paramPanelWindow.item)

        if paramPanelWindow.exec_():
            if self.observationId and set(paramPanelWindow.selectedBehaviors) != set(filtered_subjects):
                self.projectChanged = True
            self.load_subjects_in_twSubjects(paramPanelWindow.selectedBehaviors)



    def extract_events(self):
        """
        extract sequences from media file corresponding to coded events
        in case of point event, from -n to +n seconds are extracted (n = self.repositioningTimeOffset)
        """
        result, selectedObservations = self.selectObservations(MULTIPLE)

        if not selectedObservations:
            return

        plot_parameters = self.choose_obs_subj_behav_category(selectedObservations, maxTime=0, flagShowIncludeModifiers=False, flagShowExcludeBehaviorsWoEvents=False)

        if not plot_parameters["selected subjects"] or not plot_parameters["selected behaviors"]:
            return

        exportDir = QFileDialog(self).getExistingDirectory(self, "Choose a directory to extract events", os.path.expanduser("~"), options=QFileDialog(self).ShowDirsOnly)
        if not exportDir:
            return

        # check self.repositioningTimeOffset
        text, ok = QInputDialog.getDouble(self, "Offset to substract/add to start/stop times", "Time offset (in seconds):", 0.0, 0.0, 86400, 1)
        if not ok:
            return
        try:
            timeOffset = float2decimal(text)
        except:
            QMessageBox.warning(self, programName, "<b>{}</b> is not recognized as time offset".format(text))
            return

        flagUnpairedEventFound = False

        cursor = self.loadEventsInDB(plot_parameters["selected subjects"], selectedObservations, plot_parameters["selected behaviors"])

        for obsId in selectedObservations:

            for nplayer in [PLAYER1, PLAYER2]:

                if not self.pj[OBSERVATIONS][obsId][FILE][nplayer]:
                    continue

                duration1 = []   # in seconds
                for mediaFile in self.pj[OBSERVATIONS][obsId][FILE][nplayer]:
                    duration1.append(self.pj[OBSERVATIONS][obsId]["media_info"]["length"][mediaFile])

                logging.debug("duration player {}: {}".format(nplayer, duration1))

                for subject in plot_parameters["selected subjects"]:

                    for behavior in plot_parameters["selected behaviors"]:

                        cursor.execute("SELECT occurence FROM events WHERE observation = ? AND subject = ? AND code = ?",
                                       (obsId, subject, behavior))
                        rows = [{"occurence":float2decimal(r["occurence"])}  for r in cursor.fetchall()]

                        if STATE in self.eventType(behavior).upper() and len(rows) % 2:  # unpaired events
                            flagUnpairedEventFound = True
                            continue

                        for idx, row in enumerate(rows):

                            mediaFileIdx = [idx1 for idx1, x in enumerate(duration1) if row["occurence"] >= sum(duration1[0:idx1])][-1]

                            globalStart = Decimal("0.000") if row["occurence"] < timeOffset else round(row["occurence"] - timeOffset, 3)
                            start = round(row["occurence"] - timeOffset - sum(duration1[0:mediaFileIdx]), 3)
                            if start < timeOffset:
                                start = Decimal("0.000")

                            if POINT in self.eventType(behavior).upper():

                                #globalStart = Decimal("0.000") if row["occurence"] < timeOffset else round(row["occurence"] - timeOffset, 3)
                                globalStop = round(row["occurence"] + timeOffset, 3)

                                #start = round(row["occurence"] - timeOffset - sum(duration1[0:mediaFileIdx]), 3)
                                #if start < timeOffset:
                                #    start = Decimal("0.000")
                                stop = round(row["occurence"] + timeOffset - sum(duration1[0:mediaFileIdx]))

                                ffmpeg_command = '"{ffmpeg_bin}" -i "{input}" -y -ss {start} -to {stop} "{dir}{sep}{obsId}_{player}_{subject}_{behavior}_{globalStart}-{globalStop}{extension}" '.format(
                                        ffmpeg_bin=ffmpeg_bin,
                                        input=self.pj[OBSERVATIONS][obsId][FILE][nplayer][mediaFileIdx],
                                        start=start,
                                        stop=stop,
                                        globalStart=globalStart,
                                        globalStop=globalStop,
                                        dir=exportDir,
                                        sep=os.sep,
                                        obsId=obsId,
                                        player="PLAYER{}".format(nplayer),
                                        subject=subject,
                                        behavior=behavior,
                                        extension=os.path.splitext(self.pj[OBSERVATIONS][obsId][FILE][nplayer][mediaFileIdx])[-1])

                                logging.debug("ffmpeg command: {}".format( ffmpeg_command ))
                                p = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
                                out, error = p.communicate()


                            if STATE in self.eventType(behavior).upper():
                                if idx % 2 == 0:

                                    #globalStart = round(row["occurence"], 3)
                                    globalStop = round(rows[idx + 1]["occurence"] + timeOffset, 3)

                                    #start = round(row["occurence"] - sum( duration1[0:mediaFileIdx]), 3)
                                    stop = round(rows[idx + 1]["occurence"] + timeOffset - sum( duration1[0:mediaFileIdx]))

                                    # check if start after length of media
                                    if start >  self.pj[OBSERVATIONS][obsId]["media_info"]["length"][self.pj[OBSERVATIONS][obsId][FILE][nplayer][mediaFileIdx]]:
                                        print("start after end", start, self.pj[OBSERVATIONS][obsId]["media_info"]["length"][self.pj[OBSERVATIONS][obsId][FILE][nplayer][mediaFileIdx]])
                                        continue

                                    ffmpeg_command = '"{ffmpeg_bin}" -i "{input}" -y -ss {start} -to {stop} "{dir}{sep}{obsId}_{player}_{subject}_{behavior}_{globalStart}-{globalStop}{extension}" '.format(
                                    ffmpeg_bin=ffmpeg_bin,
                                    input=self.pj[OBSERVATIONS][obsId][FILE][nplayer][mediaFileIdx],
                                    start=start,
                                    stop=stop,
                                    globalStart=globalStart,
                                    globalStop=globalStop,
                                    dir=exportDir,
                                    sep=os.sep,
                                    obsId=obsId,
                                    player="PLAYER{}".format(nplayer),
                                    subject=subject,
                                    behavior=behavior,
                                    extension=os.path.splitext(self.pj[OBSERVATIONS][obsId][FILE][nplayer][mediaFileIdx])[-1])

                                    logging.debug("ffmpeg command: {}".format(ffmpeg_command))
                                    p = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
                                    out, error = p.communicate()

        self.statusbar.showMessage("Sequences extracted to {} directory".format(exportDir), 0)


    def generate_spectrogram(self):
        """
        generate spectrogram of all media files loaded in player #1
        """

        # check temp dir for images from ffmpeg
        if not self.ffmpeg_cache_dir:
            tmp_dir = tempfile.gettempdir()
        else:
            tmp_dir = self.ffmpeg_cache_dir

        w = recode_widget.Info_widget()
        w.resize(350, 100)
        w.setWindowFlags(Qt.WindowStaysOnTopHint)
        w.setWindowTitle(programName)
        w.label.setText("Generating spectrogram. Please wait...")

        for media in self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER1]:
            if os.path.isfile(media):
                process = plot_spectrogram.create_spectrogram_multiprocessing(mediaFile=media,
                                                                              tmp_dir=tmp_dir,
                                                                              chunk_size=self.chunk_length,
                                                                              ffmpeg_bin=self.ffmpeg_bin,
                                                                              spectrogramHeight=self.spectrogramHeight,
                                                                              spectrogram_color_map=self.spectrogram_color_map)


                if process:
                    w.show()
                    while True:
                        app.processEvents()
                        if not process.is_alive():
                            w.hide()
                            break

            else:
                QMessageBox.warning(self, programName , "<b>{}</b> file not found".format(media))

    def show_spectrogram(self):
        """
        show spectrogram window if any
        """

        if self.playerType == LIVE:
            QMessageBox.warning(self, programName, "The spectrogram visualization is not available for live observations")
            return

        if self.playerType == VIEWER:
            QMessageBox.warning(self, programName, "The spectrogram visualization is not available in <b>VIEW</b> mode")
            return

        if hasattr(self, "spectro"):
            self.spectro.show()
        else:
            logging.debug("spectro show not OK")

            # remember if player paused
            if self.playerType == VLC and self.playMode == VLC:
                flagPaused = self.mediaListPlayer.get_state() == vlc.State.Paused

            self.pause_video()

            if dialog.MessageDialog(programName, ("You choose to visualize the spectrogram during this observation.<br>"
                                                  "Choose YES to generate the spectrogram.\n\n"
                                                  "Spectrogram generation can take some time for long media, be patient"), [YES, NO ]) == YES:

                self.generate_spectrogram()

                if not self.ffmpeg_cache_dir:
                    tmp_dir = tempfile.gettempdir()
                else:
                    tmp_dir = self.ffmpeg_cache_dir

                currentMediaTmpPath = tmp_dir + os.sep + os.path.basename(url2path(self.mediaplayer.get_media().get_mrl()))

                logging.debug("currentMediaTmpPath {}".format(currentMediaTmpPath))

                self.pj[OBSERVATIONS][self.observationId]["visualize_spectrogram"] = True


                '''
                QMessageBox.warning(self, programName, "{}.wav.0-{}.{}.{}.spectrogram.png".format(currentMediaTmpPath,
                                                                                                       self.chunk_length,
                                                                                                       self.spectrogram_color_map,
                                                                                                       self.spectrogramHeight) )
                '''


                self.spectro = plot_spectrogram.Spectrogram("{}.wav.0-{}.{}.{}.spectrogram.png".format(currentMediaTmpPath,
                                                                                                       self.chunk_length,
                                                                                                       self.spectrogram_color_map,
                                                                                                       self.spectrogramHeight))

                # connect signal from spectrogram class to testsignal function to receive keypress events
                self.spectro.setWindowFlags(Qt.WindowStaysOnTopHint)
                self.spectro.sendEvent.connect(self.signal_from_spectrogram)
                self.spectro.show()
                self.timer_spectro.start()

            if self.playerType == VLC and self.playMode == VLC and not flagPaused:
                self.play_video()


    def timer_spectro_out(self):
        """
        timer for spectrogram visualization
        """

        if not hasattr(self, "spectro"):
            return

        if not "visualize_spectrogram" in self.pj[OBSERVATIONS][self.observationId] or not self.pj[OBSERVATIONS][self.observationId]["visualize_spectrogram"]:
            return

        if self.playerType == LIVE:
            QMessageBox.warning(self, programName, "The spectrogram visualization is not available for live observations")
            return

        if self.playerType == VLC:
            if self.playMode == VLC:

                currentMediaTime = self.mediaplayer.get_time()

            if self.playMode == FFMPEG:
                # get time in current media
                currentMedia, frameCurrentMedia = self.getCurrentMediaByFrame(PLAYER1, self.FFmpegGlobalFrame, list(self.fps.values())[0])
                currentMediaTime = frameCurrentMedia / list(self.fps.values())[0] * 1000

        currentChunk = int(currentMediaTime / 1000 / self.chunk_length)

        if currentChunk != self.spectro.memChunk:
            try:
                self.spectro.scene.removeItem(self.spectro.item)
            except:
                pass

            if not self.ffmpeg_cache_dir:
                tmp_dir = tempfile.gettempdir()
            else:
                tmp_dir = self.ffmpeg_cache_dir

            currentMediaTmpPath = tmp_dir + os.sep + os.path.basename(url2path(self.mediaplayer.get_media().get_mrl()))

            currentChunkFileName = "{}.wav.{}-{}.{}.{}.spectrogram.png".format(currentMediaTmpPath,
                                                                         currentChunk * self.chunk_length,
                                                                         (currentChunk + 1) * self.chunk_length,
                                                                         self.spectrogram_color_map,
                                                                         self.spectrogramHeight
                                                                         )

            if not os.path.isfile(currentChunkFileName):
                self.timer_spectro.stop()

                if dialog.MessageDialog(programName, ("Spectrogram file not found.<br>"
                                                      "Do you want to generate it now?<br>"
                                                      "Spectrogram generation can take some time for long media, be patient"), [YES, NO ]) == YES:

                    self.generate_spectrogram()
                    self.timer_spectro.start()

                return

            self.spectro.pixmap.load(currentChunkFileName)

            self.spectro.setWindowTitle("Spectrogram - {}".format(os.path.basename(url2path(self.mediaplayer.get_media().get_mrl()))))

            self.spectro.w, self.spectro.h = self.spectro.pixmap.width(), self.spectro.pixmap.height()

            self.spectro.item = QGraphicsPixmapItem(self.spectro.pixmap)

            self.spectro.scene.addItem(self.spectro.item)
            self.spectro.item.setPos(self.spectro.scene.width()//2, 0)

        get_time = (currentMediaTime % (self.chunk_length * 1000) / (self.chunk_length*1000))

        self.spectro.item.setPos(self.spectro.scene.width()//2 -int(get_time * self.spectro.w), 0)

        self.spectro.memChunk = currentChunk


    def map_creator(self):
        """
        show map creator window and hide program main window
        """
        self.mapCreatorWindow = map_creator.ModifiersMapCreatorWindow()
        self.mapCreatorWindow.move(self.pos())
        self.mapCreatorWindow.resize(640, 640)
        self.mapCreatorWindow.show()

    def open_observation_by_id(self,id):
        self.observationId = id

        # load events in table widget
        self.loadEventsInTW(self.observationId)

        if self.pj[OBSERVATIONS][self.observationId][TYPE] == LIVE:
            self.playerType = LIVE
            self.initialize_new_live_observation()

        if self.pj[OBSERVATIONS][self.observationId][TYPE] in [MEDIA]:

            if not self.initialize_new_observation_vlc():
                self.observationId = ""
                self.twEvents.setRowCount(0)
                self.menu_options()
                return

        self.menu_options()
        # title of dock widget  “  ”
        self.dwObservations.setWindowTitle("Events for “{}” observation".format(self.observationId))        

    def open_observation(self):
        """
        open an observation
        """

        # check if current observation must be closed to open a new one
        if self.observationId:
            response = dialog.MessageDialog(programName, "The current observation will be closed. Do you want to continue?", [YES, NO])
            if response == NO:
                return
            else:
                self.close_observation()

        result, selectedObs = self.selectObservations(OPEN)

        if selectedObs:
            self.open_observation(selectedObs[0])

    def edit_observation(self):
        """
        edit observation
        """

        # check if current observation must be closed to open a new one
        if self.observationId:
            if dialog.MessageDialog(programName, "The current observation will be closed. Do you want to continue?", [YES, NO]) == NO:
                return
            else:
                self.close_observation()

        result, selectedObs = self.selectObservations(EDIT)

        if selectedObs:
            self.new_observation(mode=EDIT, obsId=selectedObs[0])


    '''
    todo: to be deleted

    def check_state_events_old(self):
        """
        check state events for each subject in current observation
        check if number is odd
        """

        out = ""
        flagStateEvent = False

        subjects = [event[EVENT_SUBJECT_FIELD_IDX] for event in  self.pj[OBSERVATIONS][self.observationId][EVENTS]]

        for subject in sorted(set(subjects)):

            behaviors = [event[EVENT_BEHAVIOR_FIELD_IDX] for event in self.pj[OBSERVATIONS][self.observationId][EVENTS] if event[EVENT_SUBJECT_FIELD_IDX] == subject]

            for behavior in sorted(set(behaviors)):
                if "STATE" in self.eventType(behavior).upper():
                    flagStateEvent = True

                    behavior_modifiers = [behav + "@@@" + mod for _, subj, behav, mod, _ in  self.pj[OBSERVATIONS][self.observationId][EVENTS] if behav == behavior and subj == subject]

                    for behavior_modifier in set(behavior_modifiers):

                        if behavior_modifiers.count(behavior_modifier) % 2:
                            if subject:
                                subject = " for subject <b>{}</b>".format(subject)
                            modifier = behavior_modifier.split("@@@")[1]
                            if modifier:
                                modifier = "(modifier <b>{}</b>)".format(modifier)
                            out += "The behavior <b>{0}</b> {1} is not PAIRED {2}<br>".format(behavior, modifier, subject)

        if not out:
            out = "State events are PAIRED"
        if flagStateEvent:
            QMessageBox.warning(self, programName + " - State events check", out)
        else:
            QMessageBox.warning(self, programName + " - State events check", "No state events in current observation")
    '''


    def check_state_events(self):
        """
        check state events for each subject in current observation
        if no current observation check all observations
        check if number is odd
        """

        def check_state_events_obs(obsId):
            out = ""
            flagStateEvent = False
            subjects = [event[EVENT_SUBJECT_FIELD_IDX] for event in  self.pj[OBSERVATIONS][obsId][EVENTS]]

            for subject in sorted(set(subjects)):

                behaviors = [event[EVENT_BEHAVIOR_FIELD_IDX] for event in self.pj[OBSERVATIONS][obsId][EVENTS] if event[EVENT_SUBJECT_FIELD_IDX] == subject]

                for behavior in sorted(set(behaviors)):
                    if "STATE" in self.eventType(behavior).upper():
                        flagStateEvent = True
                        lst, memTime = [], {}
                        for event in [event for event in self.pj[OBSERVATIONS][obsId][EVENTS] if event[EVENT_BEHAVIOR_FIELD_IDX] == behavior and event[EVENT_SUBJECT_FIELD_IDX] == subject]:
                            behav_modif = [event[EVENT_BEHAVIOR_FIELD_IDX], event[EVENT_MODIFIER_FIELD_IDX]]
                            if behav_modif in lst:
                                lst.remove(behav_modif)
                                del memTime[str(behav_modif)]
                            else:
                                lst.append(behav_modif)
                                memTime[str(behav_modif)] = event[EVENT_TIME_FIELD_IDX]

                        for event in lst:
                            out += """The behavior <b>{behavior}</b> {modifier}is not PAIRED for subject "<b>{subject}</b>" at <b>{time}</b><br>""".format(
                                   behavior=behavior,
                                   modifier=("(modifier "+ event[1] + ") ") if event[1] else "",
                                   subject=subject if subject else NO_FOCAL_SUBJECT,
                                   time=memTime[str(event)] if self.timeFormat == S else seconds2time(memTime[str(event)]))

            return out

        if self.observationId:
            r = check_state_events_obs(self.observationId)
            if not r:
                r = "All state events are PAIRED"
            tot_out = "<strong>{0}</strong><br>{1}<br>".format(self.observationId, r)
        else: # no current observation

             # ask user observations to analyze
            _, selectedObservations = self.selectObservations(MULTIPLE)
            if not selectedObservations:
                return

            tot_out = ""
            for obsId in sorted(selectedObservations):
                r = check_state_events_obs(obsId)
                if not r:
                    r = "All state events are PAIRED"
                tot_out += "<strong>{0}</strong><br>{1}<br>".format(obsId, r)


        self.results = dialog.ResultsWidget()
        self.results.setWindowTitle("Check state events")
        self.results.ptText.clear()
        self.results.ptText.appendHtml(tot_out)
        self.results.show()


    def observations_list(self):
        """
        view all observations
        """
        # check if an observation is running
        if self.observationId:
            QMessageBox.critical(self, programName, "You must close the running observation before.")
            return

        result, selectedObs = self.selectObservations(SINGLE)

        if selectedObs:

            if result == OPEN:

                self.observationId = selectedObs[0]

                # load events in table widget
                self.loadEventsInTW(self.observationId)

                if self.pj[OBSERVATIONS][self.observationId][TYPE] == LIVE:
                    self.playerType = LIVE
                    self.initialize_new_live_observation()

                if self.pj[OBSERVATIONS][self.observationId][TYPE] in [MEDIA]:

                    if not self.initialize_new_observation_vlc():
                        self.observationId = ''
                        self.twEvents.setRowCount(0)
                        self.menu_options()

                self.menu_options()
                # title of dock widget
                self.dwObservations.setWindowTitle("Events for “{}” observation".format(self.observationId))


            if result == EDIT:

                if self.observationId != selectedObs[0]:
                    self.new_observation( mode=EDIT, obsId=selectedObs[0])   # observation id to edit
                else:
                    QMessageBox.warning(self, programName , 'The observation <b>%s</b> is running!<br>Close it before editing.' % self.observationId)


    def actionCheckUpdate_activated(self, flagMsgOnlyIfNew = False):
        """
        check BORIS web site for updates
        """
        try:
            versionURL = "http://www.boris.unito.it/static/ver.dat"
            lastVersion = Decimal(urllib.request.urlopen(versionURL).read().strip().decode("utf-8"))
            self.saveConfigFile(lastCheckForNewVersion = int(time.mktime(time.localtime())))

            if lastVersion > Decimal(__version__):
                msg = """A new version is available: v. <b>{}</b><br>Go to <a href="http://www.boris.unito.it">http://www.boris.unito.it</a> to install it.""".format(lastVersion)
            else:
                msg = "The version you are using is the last one: <b>{}</b>".format(__version__)

            QMessageBox.information(self, programName, msg)

        except:
            QMessageBox.warning(self, programName, "Can not check for updates...")


    def jump_to(self):
        """
        jump to the user specified media position
        """

        jt = dialog.JumpTo(self.timeFormat)

        if jt.exec_():
            if self.timeFormat == HHMMSS:
                newTime = int(time2seconds(jt.te.time().toString(HHMMSSZZZ)) * 1000)
            else:
                newTime = int( jt.te.value() * 1000)

            if self.playerType == VLC:
                if self.playMode == FFMPEG:
                    frameDuration = Decimal(1000 / list(self.fps.values())[0])
                    currentFrame = round(newTime / frameDuration)
                    self.FFmpegGlobalFrame = currentFrame

                    if self.second_player():
                        currentFrame2 = round(newTime / frameDuration)
                        self.FFmpegGlobalFrame2 = currentFrame2

                    if self.FFmpegGlobalFrame > 0:
                        self.FFmpegGlobalFrame -= 1
                        if self.FFmpegGlobalFrame2 > 0:
                            self.FFmpegGlobalFrame2 -= 1
                    self.ffmpegTimerOut()

                else: # play mode VLC

                    if self.media_list.count() == 1:

                        if newTime < self.mediaplayer.get_length():
                            self.mediaplayer.set_time( newTime )
                            if self.simultaneousMedia:
                                self.mediaplayer2.set_time( int(self.mediaplayer.get_time()  - self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] * 1000) )

                        else:
                            QMessageBox.warning(self, programName , "The indicated position is behind the end of media ({})".format(seconds2time(self.mediaplayer.get_length()/1000)))

                    elif self.media_list.count() > 1:

                        if newTime  < sum(self.duration):

                            # remember if player paused (go previous will start playing)
                            flagPaused = self.mediaListPlayer.get_state() == vlc.State.Paused

                            tot = 0
                            for idx, d in enumerate(self.duration):
                                if newTime >= tot and newTime < tot + d:
                                    self.mediaListPlayer.play_item_at_index(idx)

                                    # wait until media is played
                                    while True:
                                        if self.mediaListPlayer.get_state() in [vlc.State.Playing, vlc.State.Ended]:
                                            break

                                    if flagPaused:
                                        self.mediaListPlayer.pause()

                                    self.mediaplayer.set_time(newTime - sum(self.duration[0 : self.media_list.index_of_item(self.mediaplayer.get_media())]))

                                    break
                                tot += d
                        else:
                            QMessageBox.warning(self, programName, "The indicated position is behind the total media duration ({})".format(seconds2time(sum(self.duration)/1000)))

                    self.timer_out()
                    self.timer_spectro_out()


    def previous_media_file(self):
        """
        go to previous media file (if any)
        """
        if len(self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER1]) == 1:
            return

        if self.playerType == VLC:

            if self.playMode == FFMPEG:

                currentMedia = ""
                for idx, media in enumerate(self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER1]):
                    if self.FFmpegGlobalFrame < self.duration[idx + 1]:
                        self.FFmpegGlobalFrame = self.duration[idx - 1]
                        break
                self.FFmpegGlobalFrame -= 1
                self.ffmpegTimerOut()

            else:

                # check if media not first media
                if self.media_list.index_of_item(self.mediaplayer.get_media()) > 0:

                    # remember if player paused (go previous will start playing)
                    flagPaused = self.mediaListPlayer.get_state() == vlc.State.Paused
                    self.mediaListPlayer.previous()

                    while True:
                        if self.mediaListPlayer.get_state() in [vlc.State.Playing, vlc.State.Ended]:
                            break

                    if flagPaused:
                        self.mediaListPlayer.pause()
                else:

                    if self.media_list.count() == 1:
                        self.statusbar.showMessage("There is only one media file", 5000)
                    else:
                        if self.media_list.index_of_item(self.mediaplayer.get_media()) == 0:
                            self.statusbar.showMessage("The first media is playing", 5000)

                self.timer_out()
                self.timer_spectro_out()

                # no subtitles
                #self.mediaplayer.video_set_spu(0)

            if hasattr(self, "spectro"):
                self.spectro.memChunk = -1


    def next_media_file(self):
        """
        go to next media file (if any)
        """
        if len(self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER1]) == 1:
            return

        if self.playerType == VLC:

            if self.playMode == FFMPEG:
                for idx, media in enumerate(self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER1]):
                    if self.FFmpegGlobalFrame < self.duration[idx + 1]:
                        self.FFmpegGlobalFrame = self.duration[idx + 1]
                        break
                self.FFmpegGlobalFrame -= 1
                self.ffmpegTimerOut()

            else:

                # check if media not last media
                if self.media_list.index_of_item(self.mediaplayer.get_media()) <  self.media_list.count() - 1:

                    # remember if player paused (go previous will start playing)
                    flagPaused = self.mediaListPlayer.get_state() == vlc.State.Paused

                    next(self.mediaListPlayer)

                    # wait until media is played
                    while True:
                        if self.mediaListPlayer.get_state() in [vlc.State.Playing, vlc.State.Ended]:
                            break

                    if flagPaused:
                        logging.info("media player state: {0}".format(self.mediaListPlayer.get_state()))
                        self.mediaListPlayer.pause()

                else:
                    if self.media_list.count() == 1:
                        self.statusbar.showMessage("There is only one media file", 5000)
                    else:
                        if self.media_list.index_of_item(self.mediaplayer.get_media()) == self.media_list.count() - 1:
                            self.statusbar.showMessage("The last media is playing", 5000)


                self.timer_out()
                self.timer_spectro_out()
                # no subtitles
                #self.mediaplayer.video_set_spu(0)

            if hasattr(self, "spectro"):
                self.spectro.memChunk = -1


    def setVolume(self):
        """
        set volume for player #1
        """

        self.mediaplayer.audio_set_volume( self.volumeslider.value())

    def setVolume2(self):
        """
        set volume for player #2
        """

        self.mediaplayer2.audio_set_volume(self.volumeslider2.value())


    def automatic_backup(self):
        """
        save project every x minutes if current observation
        """

        if self.observationId:
            logging.info("automatic backup")
            self.save_project_activated()


    def deselectSubject(self):
        """
        deselect the current subject
        """
        self.currentSubject = ""
        self.lbSubject.setText( "<b>{}</b>".format(NO_FOCAL_SUBJECT))
        self.lbFocalSubject.setText(NO_FOCAL_SUBJECT)

    def selectSubject(self, subject):
        """
        deselect the current subject
        """
        self.currentSubject = subject
        self.lbSubject.setText("Subject: <b>{}</b>".format(self.currentSubject))
        self.lbFocalSubject.setText(" Focal subject: <b>{}</b>".format(self.currentSubject))

    def preferences(self):
        """
        show preferences window
        """

        preferencesWindow = preferences.Preferences()
        preferencesWindow.tabWidget.setCurrentIndex(0)

        if self.timeFormat == S:
            preferencesWindow.cbTimeFormat.setCurrentIndex(0)

        if self.timeFormat == HHMMSS:
            preferencesWindow.cbTimeFormat.setCurrentIndex(1)

        preferencesWindow.sbffSpeed.setValue(self.fast)
        preferencesWindow.sbRepositionTimeOffset.setValue(self.repositioningTimeOffset)
        preferencesWindow.sbSpeedStep.setValue( self.play_rate_step)
        # automatic backup
        preferencesWindow.sbAutomaticBackup.setValue(self.automaticBackup)
        # separator for behavioural strings
        preferencesWindow.leSeparator.setText(self.behaviouralStringsSeparator)
        # close same event indep of modifiers
        preferencesWindow.cbCloseSameEvent.setChecked(self.close_the_same_current_event)
        # confirm sound
        preferencesWindow.cbConfirmSound.setChecked(self.confirmSound)
        # beep every
        preferencesWindow.sbBeepEvery.setValue(self.beep_every)
        # embed player
        preferencesWindow.cbEmbedPlayer.setChecked(self.embedPlayer)
        # alert no focal subject
        preferencesWindow.cbAlertNoFocalSubject.setChecked(self.alertNoFocalSubject)
        # tracking cursor above event
        preferencesWindow.cbTrackingCursorAboveEvent.setChecked(self.trackingCursorAboveEvent)
        # check for new version
        preferencesWindow.cbCheckForNewVersion.setChecked(self.checkForNewVersion)

        # FFmpeg for frame by frame mode
        preferencesWindow.lbFFmpegPath.setText("FFmpeg path: {}".format(self.ffmpeg_bin))
        preferencesWindow.leFFmpegCacheDir.setText(self.ffmpeg_cache_dir)
        preferencesWindow.sbFFmpegCacheDirMaxSize.setValue(self.ffmpeg_cache_dir_max_size)

        # frame-by-frame mode
        preferencesWindow.sbFrameResize.setValue(self.frame_resize)
        mem_frame_resize = self.frame_resize

        preferencesWindow.cbFrameBitmapFormat.clear()
        preferencesWindow.cbFrameBitmapFormat.addItems(FRAME_BITMAP_FORMAT_LIST)

        try:
            preferencesWindow.cbFrameBitmapFormat.setCurrentIndex(FRAME_BITMAP_FORMAT_LIST.index(self.frame_bitmap_format))
        except:
            preferencesWindow.cbFrameBitmapFormat.setCurrentIndex(FRAME_BITMAP_FORMAT_LIST.index(FRAME_DEFAULT_BITMAT_FORMAT))


        preferencesWindow.cbDetachFrameViewer.setChecked(self.detachFrameViewer)

        # spectrogram
        preferencesWindow.sbSpectrogramHeight.setValue(self.spectrogramHeight)

        preferencesWindow.cbSpectrogramColorMap.clear()
        preferencesWindow.cbSpectrogramColorMap.addItems(SPECTROGRAM_COLOR_MAPS)

        try:
            preferencesWindow.cbSpectrogramColorMap.setCurrentIndex(SPECTROGRAM_COLOR_MAPS.index(self.spectrogram_color_map))
        except:
            preferencesWindow.cbSpectrogramColorMap.setCurrentIndex(SPECTROGRAM_COLOR_MAPS.index(SPECTROGRAM_DEFAULT_COLOR_MAP))


        if preferencesWindow.exec_():

            if preferencesWindow.cbTimeFormat.currentIndex() == 0:
                self.timeFormat = S

            if preferencesWindow.cbTimeFormat.currentIndex() == 1:
                self.timeFormat = HHMMSS

            self.fast = preferencesWindow.sbffSpeed.value()

            self.repositioningTimeOffset = preferencesWindow.sbRepositionTimeOffset.value()

            self.play_rate_step = preferencesWindow.sbSpeedStep.value()

            self.automaticBackup = preferencesWindow.sbAutomaticBackup.value()
            if self.automaticBackup:
                self.automaticBackupTimer.start(self.automaticBackup * 60000)
            else:
                self.automaticBackupTimer.stop()

            self.behaviouralStringsSeparator = preferencesWindow.leSeparator.text()

            self.close_the_same_current_event = preferencesWindow.cbCloseSameEvent.isChecked()

            self.confirmSound = preferencesWindow.cbConfirmSound.isChecked()

            self.beep_every = preferencesWindow.sbBeepEvery.value()

            self.embedPlayer = preferencesWindow.cbEmbedPlayer.isChecked()

            self.alertNoFocalSubject = preferencesWindow.cbAlertNoFocalSubject.isChecked()

            self.trackingCursorAboveEvent = preferencesWindow.cbTrackingCursorAboveEvent.isChecked()

            self.checkForNewVersion = preferencesWindow.cbCheckForNewVersion.isChecked()

            if self.observationId:
                self.loadEventsInTW( self.observationId )
                self.display_timeoffset_statubar(self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET])

            self.ffmpeg_cache_dir = preferencesWindow.leFFmpegCacheDir.text()
            self.ffmpeg_cache_dir_max_size = preferencesWindow.sbFFmpegCacheDirMaxSize.value()

            # frame-by-frame
            self.frame_resize = preferencesWindow.sbFrameResize.value()

            # delete files in imageDirectory f frame_resize changed
            if self.frame_resize != mem_frame_resize:
                # check temp dir for images from ffmpeg
                if not self.ffmpeg_cache_dir:
                    self.imageDirectory = tempfile.gettempdir()
                else:
                    self.imageDirectory = self.ffmpeg_cache_dir

                for f in [x for x in os.listdir(self.imageDirectory) if "BORIS@" in x and os.path.isfile(self.imageDirectory + os.sep + x)]:
                    try:
                        os.remove(self.imageDirectory + os.sep + f)
                    except:
                        pass

            self.frame_bitmap_format = preferencesWindow.cbFrameBitmapFormat.currentText()

            # detach frame viewer
            self.detachFrameViewer = preferencesWindow.cbDetachFrameViewer.isChecked()

            # spectrogram
            self.spectrogram_color_map = preferencesWindow.cbSpectrogramColorMap.currentText()
            self.spectrogramHeight = preferencesWindow.sbSpectrogramHeight.value()

            if self.playMode == FFMPEG:
                if self.detachFrameViewer:
                    if hasattr(self, "lbFFmpeg"):
                        self.lbFFmpeg.clear()
                    if self.observationId and self.playerType == VLC and self.playMode == FFMPEG:
                        self.create_frame_viewer()
                        self.FFmpegGlobalFrame -= 1
                        self.ffmpegTimerOut()
                else:
                    if hasattr(self, "frame_viewer1"):
                        self.frame_viewer1_mem_geometry = self.frame_viewer1.geometry()
                        del self.frame_viewer1
                    self.FFmpegGlobalFrame -= 1

                    if self.second_player():
                        if hasattr(self, "frame_viewer2"):
                            self.frame_viewer2_mem_geometry = self.frame_viewer2.geometry()
                            del self.frame_viewer2
                        self.FFmpegGlobalFrame2 -= 1

                    self.ffmpegTimerOut()

            self.menu_options()

            self.saveConfigFile()


    def getCurrentMediaByFrame(self, player, requiredFrame, fps):
        """
        get:
        player
        required frame
        fps

        returns:
        currentMedia
        frameCurrentMedia
        """
        currentMedia, frameCurrentMedia = "", 0
        frameMs = 1000 / fps
        for idx, media in enumerate(self.pj[OBSERVATIONS][self.observationId][FILE][player]):
            if requiredFrame * frameMs < sum(self.duration[0:idx + 1]):
                currentMedia = media
                frameCurrentMedia = requiredFrame - sum(self.duration[0:idx]) / frameMs
                break
        return currentMedia, round(frameCurrentMedia)


    def getCurrentMediaByTime(self, player, obsId, globalTime):
        """
        get:
        player
        globalTime

        returns:
        currentMedia
        frameCurrentMedia
        """
        currentMedia, currentMediaTime = '', 0

        globalTimeMs = globalTime * 1000

        for idx, media in enumerate(self.pj[OBSERVATIONS][obsId][FILE][player]):
            if globalTimeMs < sum(self.duration[0:idx + 1]):
                currentMedia = media
                currentMediaTime = globalTimeMs - sum(self.duration[0:idx])
                break

        return currentMedia, round(currentMediaTime/1000,3)

    def second_player(self):
        """

        :return: True if second player else False
        """
        if not self.observationId:
            return False
        if (PLAYER2 in self.pj[OBSERVATIONS][self.observationId][FILE] and
                self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER2]):
            return True
        else:
            return False

    def create_frame_viewer(self):
        """
        create frame viewer
        if 2nd player 2nd frame viewer is created
        """
        if not hasattr(self, "frame_viewer1"):
            self.frame_viewer1 = dialog.FrameViewer()
            self.frame_viewer1.setWindowTitle("Frame viewer #1")
            self.frame_viewer1.setWindowFlags(Qt.WindowStaysOnTopHint)
            if self.frame_viewer1_mem_geometry:
                self.frame_viewer1.setGeometry(self.frame_viewer1_mem_geometry)
            else:
                self.frame_viewer1.setGeometry(100, 100, 256, 256)

        if self.second_player():
            if not hasattr(self, "frame_viewer2"):
                self.frame_viewer2 = dialog.FrameViewer()
                self.frame_viewer2.setWindowTitle("Frame viewer #2")
                self.frame_viewer2.setWindowFlags(Qt.WindowStaysOnTopHint)
                if self.frame_viewer2_mem_geometry:
                    self.frame_viewer2.setGeometry(self.frame_viewer2_mem_geometry)
                else:
                    self.frame_viewer2.setGeometry(150, 150, 256, 256)



    def ffmpegTimerOut(self):
        """
        triggered when frame-by-frame mode is activated:
        read next frame and update image
        """

        logging.debug("FFmpegTimerOut function")

        #global BITMAP_EXT

        fps = list(self.fps.values())[0]

        logging.debug("fps {0}".format(fps))

        frameMs = 1000 / fps

        logging.debug("framMs {0}".format(frameMs))

        requiredFrame = self.FFmpegGlobalFrame + 1

        logging.debug("required frame 1: {0}".format(requiredFrame))
        logging.debug("sum self.duration1 {0}".format(sum(self.duration)))

        # check if end of last media
        if requiredFrame * frameMs >= sum(self.duration):
            logging.debug("end of last media 1 frame: {}".format(requiredFrame))
            return

        currentMedia, frameCurrentMedia = self.getCurrentMediaByFrame(PLAYER1, requiredFrame, fps)
        logging.debug("frame current media 1: {}".format(frameCurrentMedia))
        #logging.debug("int(frameCurrentMedia1 / fps): {}".format(int(frameCurrentMedia / fps)))

        if "visualize_spectrogram" in self.pj[OBSERVATIONS][self.observationId] and self.pj[OBSERVATIONS][self.observationId]["visualize_spectrogram"]:
            self.timer_spectro_out()

        md5FileName = hashlib.md5(currentMedia.encode("utf-8")).hexdigest()


        if "BORIS@{md5FileName}-{second}".format(md5FileName=md5FileName, second=int(frameCurrentMedia / fps)) not in self.imagesList:

            extract_frames(self.ffmpeg_bin, int(frameCurrentMedia / fps), currentMedia, str(round(fps) +1), self.imageDirectory, md5FileName, self.frame_bitmap_format.lower(), self.frame_resize)

            self.imagesList.update([f.replace(self.imageDirectory + os.sep, "").split("_")[0] for f in glob.glob(self.imageDirectory + os.sep + "BORIS@*")])


        logging.debug("images 1 list: {}".format(self.imagesList))

        second1 = int((frameCurrentMedia -1 )/ fps)
        frame1 = round((frameCurrentMedia - int((frameCurrentMedia -1)/ fps) * fps))
        if frame1 == 0:
            frame1 += 1
        logging.debug("second1: {}  frame1: {}".format(second1,frame1))
        #logging.debug("image 1 {}".format("{}-{} {}".format(md5FileName, int(frameCurrentMedia / fps), frame1)))

        img = "{imageDir}{sep}BORIS@{fileName}-{second}_{frame}.{extension}".format(imageDir=self.imageDirectory,
                                                                                    sep=os.sep,
                                                                                    fileName=md5FileName,
                                                                                    second=second1,
                                                                                    frame=frame1,
                                                                                    extension=self.frame_bitmap_format.lower())

        logging.debug("image1: {}".format(img))
        if not os.path.isfile(img):
            logging.warning("image 1 not found: {0}".format(img))
            extract_frames(self.ffmpeg_bin, int(frameCurrentMedia / fps), currentMedia, str(round(fps) + 1), self.imageDirectory, md5FileName, self.frame_bitmap_format.lower(), self.frame_resize)
            if not os.path.isfile(img):
                logging.warning("image 1 still not found: {0}".format(img))
                return

        self.pixmap = QPixmap(img)
        # check if jpg filter available if not use png
        if self.pixmap.isNull():
            self.frame_bitmap_format = "PNG"

        if self.second_player():

            requiredFrame2 = self.FFmpegGlobalFrame2 + 1

            if TIME_OFFSET_SECOND_PLAYER in self.pj[OBSERVATIONS][self.observationId]:

                # sync 2nd player on 1st player when no offset
                if self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] == 0:
                    self.FFmpegGlobalFrame2  = self.FFmpegGlobalFrame
                    requiredFrame2 = requiredFrame

                if self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] > 0:

                    if requiredFrame < self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] * fps:
                        requiredFrame2 = 1
                    else:
                        requiredFrame2 = int(requiredFrame - self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] * fps)

                if self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] < 0:

                    if requiredFrame2 < abs(self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] * fps):
                        requiredFrame = 1
                    else:
                        requiredFrame = int(requiredFrame2 + self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] * fps)

            currentMedia2, frameCurrentMedia2 = self.getCurrentMediaByFrame(PLAYER2, requiredFrame2, fps)
            md5FileName2 = hashlib.md5(currentMedia2.encode("utf-8")).hexdigest()
            if "BORIS@{md5FileName}-{second}".format(md5FileName=md5FileName2,
                                                     second=int(frameCurrentMedia2 / fps)) not in self.imagesList:
                extract_frames(self.ffmpeg_bin, int(frameCurrentMedia2 / fps), currentMedia2, str(round(fps) + 1),
                               self.imageDirectory, md5FileName2, self.frame_bitmap_format.lower(), self.frame_resize)

                self.imagesList.update([f.replace(self.imageDirectory + os.sep, "").split("_")[0] for f in
                                        glob.glob(self.imageDirectory + os.sep + "BORIS@*")])

            second2 = int((frameCurrentMedia2 - 1) / fps)
            frame2 = round((frameCurrentMedia2 - int((frameCurrentMedia2 -1)/ fps) * fps))
            if frame2 == 0:
                frame2 += 1

            img2 = "{imageDir}{sep}BORIS@{fileName}-{second}_{frame}.{extension}".format(imageDir=self.imageDirectory,
                                                                                        sep=os.sep,
                                                                                        fileName=md5FileName2,
                                                                                        second=second2,
                                                                                        frame=frame2,
                                                                                        extension=self.frame_bitmap_format.lower())
            if not os.path.isfile(img2):
                logging.warning("image 2 not found: {0}".format(img2))
                extract_frames(self.ffmpeg_bin, int(frameCurrentMedia2 / fps), currentMedia2, str(round(fps) +1), self.imageDirectory, md5FileName2, self.frame_bitmap_format.lower(), self.frame_resize)
                if not os.path.isfile(img2):
                    logging.warning("image 2 still not found: {0}".format(img2))
                    return
            self.pixmap2 = QPixmap(img2)

        if self.detachFrameViewer or self.second_player():   # frame viewer detached or 2 players
            self.create_frame_viewer()

            self.frame_viewer1.show()
            if self.second_player():
                self.frame_viewer2.show()

            self.frame_viewer1.lbFrame.setPixmap(self.pixmap.scaled(self.frame_viewer1.lbFrame.size(), Qt.KeepAspectRatio))
            if self.second_player():
                self.frame_viewer2.lbFrame.setPixmap(self.pixmap2.scaled(self.frame_viewer2.lbFrame.size(), Qt.KeepAspectRatio))

        elif not self.detachFrameViewer:
            self.lbFFmpeg.setPixmap(self.pixmap.scaled(self.lbFFmpeg.size(), Qt.KeepAspectRatio))

        # redraw measurements from previous frames
        if self.measurement_w:
            if self.measurement_w.cbPersistentMeasurements.isChecked():
                for frame in self.measurement_w.draw_mem:

                    if frame == self.FFmpegGlobalFrame + 1:
                        elementsColor = "lime"
                    else:
                        elementsColor = "red"

                    for element in self.measurement_w.draw_mem[frame]:
                        if element[0] == "line":
                            x1, y1, x2, y2 = element[1:]
                            self.draw_line(x1, y1, x2, y2, elementsColor)
                            self.draw_point(x1, y1, elementsColor)
                            self.draw_point(x2, y2, elementsColor)
                        if element[0] == "angle":
                            x1, y1 = element[1][0]
                            x2, y2 = element[1][1]
                            x3, y3 = element[1][2]
                            self.draw_line(x1, y1, x2, y2, elementsColor)
                            self.draw_line(x1, y1, x3, y3, elementsColor)
                            self.draw_point(x1, y1, elementsColor)
                            self.draw_point(x2, y2, elementsColor)
                            self.draw_point(x3, y3, elementsColor)
                        if element[0] == "polygon":
                            polygon = QPolygon()
                            for point in element[1]:
                                polygon.append(QPoint(point[0], point[1]))
                            painter = QPainter()
                            painter.begin(self.lbFFmpeg.pixmap())
                            painter.setPen(QColor(elementsColor))
                            painter.drawPolygon(polygon)
                            painter.end()
                            self.lbFFmpeg.update()
            else:
                self.measurement_w.draw_mem = []

        self.FFmpegGlobalFrame = requiredFrame
        if self.second_player():
            self.FFmpegGlobalFrame2 = requiredFrame2

        currentTime = self.getLaps() * 1000

        self.lbTime.setText("{currentMediaName}: <b>{currentTime} / {totalTime}</b> frame: <b>{currentFrame}</b>".format(
                             currentMediaName=os.path.basename(currentMedia),
                             currentTime=self.convertTime(currentTime / 1000),
                             totalTime=self.convertTime(Decimal(self.mediaplayer.get_length() / 1000)),
                             currentFrame=round(self.FFmpegGlobalFrame)
                             ))

        # extract State events
        StateBehaviorsCodes = [self.pj[ETHOGRAM][x]["code"] for x in [y for y in self.pj[ETHOGRAM]
                                if "State" in self.pj[ETHOGRAM][y][TYPE]]]

        self.currentStates = {}

        # add states for no focal subject
        self.currentStates[""] = []
        for sbc in StateBehaviorsCodes:
            if len([x[ pj_obs_fields["code"]] for x in self.pj[OBSERVATIONS][self.observationId][EVENTS]
                       if x[pj_obs_fields["subject"]] == "" and x[pj_obs_fields["code"]] == sbc and x[pj_obs_fields["time"]] <= currentTime /1000]) % 2: # test if odd
                self.currentStates[""].append(sbc)

        # add states for all configured subjects
        for idx in self.pj[SUBJECTS]:

            # add subject index
            self.currentStates[ idx ] = []
            for sbc in StateBehaviorsCodes:
                if len([x[pj_obs_fields["code"]] for x in self.pj[OBSERVATIONS][self.observationId][EVENTS]
                           if x[pj_obs_fields["subject"]] == self.pj[SUBJECTS][idx]["name"] and x[pj_obs_fields["code"]] == sbc and x[pj_obs_fields["time"]] <= currentTime / 1000 ]) % 2: # test if odd
                    self.currentStates[idx].append(sbc)

        # show current states
        if self.currentSubject:
            # get index of focal subject (by name)
            idx = [idx for idx in self.pj[SUBJECTS] if self.pj[SUBJECTS][idx]['name'] == self.currentSubject][0]
            self.lbCurrentStates.setText("%s" % (", ".join(self.currentStates[ idx ])))
        else:
            self.lbCurrentStates.setText("%s" % (", ".join(self.currentStates[""])))

        # show selected subjects
        for idx in sorted_keys(self.pj[SUBJECTS]):  #    [str(x) for x in sorted([int(x) for x in self.pj[SUBJECTS].keys()])]:
            self.twSubjects.item(int(idx), len( subjectsFields ) ).setText(",".join(self.currentStates[idx]) )

        # show tracking cursor
        self.get_events_current_row()


    def close_measurement_widget(self):
        self.measurement_w.close()
        self.measurement_w = None

    def clear_measurements(self):
        if self.FFmpegGlobalFrame > 1:
            self.FFmpegGlobalFrame -= 1
            self.ffmpegTimerOut()

    def distance(self):
        """
        active the geometric measurement window
        """
        import measurement_widget
        self.measurement_w = measurement_widget.wgMeasurement(logging.getLogger().getEffectiveLevel())
        self.measurement_w.draw_mem = {}
        self.measurement_w.setWindowFlags(Qt.WindowStaysOnTopHint)
        self.measurement_w.closeSignal.connect(self.close_measurement_widget)
        self.measurement_w.clearSignal.connect(self.clear_measurements)

        self.measurement_w.show()


    def draw_point(self, x, y, color):
        """
        draw point on frame-by-frame image
        """
        RADIUS = 6
        painter = QPainter()
        painter.begin(self.lbFFmpeg.pixmap())
        painter.setPen(QColor(color))
        painter.drawEllipse(QPoint(x, y), RADIUS, RADIUS)
        # cross inside circle
        painter.drawLine(x - RADIUS, y, x + RADIUS, y)
        painter.drawLine(x, y - RADIUS, x, y + RADIUS)
        painter.end()
        self.lbFFmpeg.update()


    def draw_line(self, x1, y1, x2, y2, color):
        """
        draw line on frame-by-frame image
        """
        painter = QPainter()
        painter.begin(self.lbFFmpeg.pixmap())
        painter.setPen(QColor(color))
        painter.drawLine(x1, y1, x2, y2)
        painter.end()
        self.lbFFmpeg.update()


    def getPoslbFFmpeg(self, event):
        """
        return click position on frame and distance between 2 last clicks
        """
        if self.measurement_w:
            x = event.pos().x()
            y = event.pos().y()

            # distance
            if self.measurement_w.rbDistance.isChecked():
                if event.button() == 1:   # left
                    self.draw_point(x, y, "lime")
                    self.memx, self.memy = x, y

                if event.button() == 2 and self.memx != -1 and self.memy != -1:
                    self.draw_point(x, y, "lime")
                    self.draw_line(self.memx, self.memy, x, y, "lime")

                    if self.FFmpegGlobalFrame in self.measurement_w.draw_mem:
                        self.measurement_w.draw_mem[self.FFmpegGlobalFrame].append(["line", self.memx, self.memy, x, y])
                    else:
                        self.measurement_w.draw_mem[self.FFmpegGlobalFrame] = [["line", self.memx, self.memy, x, y]]

                    d = ((x - self.memx) ** 2 + (y - self.memy) ** 2) ** 0.5
                    try:
                        d = d / float(self.measurement_w.lePx.text()) * float(self.measurement_w.leRef.text())
                    except:
                        QMessageBox.critical(self, programName,
                                             "Check reference and pixel values! Values must be numeric.",
                                             QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)

                    self.measurement_w.pte.appendPlainText("Time: {}\tFrame: {}\tDistance: {}".format(self.getLaps(),
                                                                                                         self.FFmpegGlobalFrame,
                                                                                                         round(d, 1)))
                    self.measurement_w.flagSaved = False
                    self.memx, self.memy = -1, -1

            # angle 1st clic -> vertex
            if self.measurement_w.rbAngle.isChecked():
                if event.button() == 1:   # left for vertex
                    self.draw_point(x, y, "lime")
                    self.memPoints = [(x, y)]

                if event.button() == 2 and len(self.memPoints):
                    self.draw_point(x, y, "lime")
                    self.draw_line(self.memPoints[0][0], self.memPoints[0][1], x, y, "lime")

                    self.memPoints.append((x, y))

                    if len( self.memPoints ) == 3:
                        self.measurement_w.pte.appendPlainText("Time: {}\tFrame: {}\tAngle: {}".format(self.getLaps(),
                                                                                                      self.FFmpegGlobalFrame,
                                                                                                      round(angle(self.memPoints[0], self.memPoints[1], self.memPoints[2]), 1)
                                                                                                      ))
                        self.measurement_w.flagSaved = False
                        if self.FFmpegGlobalFrame in self.measurement_w.draw_mem:
                            self.measurement_w.draw_mem[self.FFmpegGlobalFrame].append(["angle", self.memPoints])
                        else:
                            self.measurement_w.draw_mem[self.FFmpegGlobalFrame] = [["angle", self.memPoints]]

                        self.memPoints = []

            # Area
            if self.measurement_w.rbArea.isChecked():
                if event.button() == 1:   # left
                    self.draw_point(x, y, "lime")
                    if len(self.memPoints):
                        self.draw_line(self.memPoints[-1][0], self.memPoints[-1][1], x, y, "lime")
                    self.memPoints.append((x, y))

                if event.button() == 2 and len(self.memPoints) >= 2:
                    self.draw_point(x, y, "lime")
                    self.draw_line(self.memPoints[-1][0], self.memPoints[-1][1], x, y, "lime")
                    self.memPoints.append((x, y))
                    # close polygon
                    self.draw_line(self.memPoints[-1][0], self.memPoints[-1][1], self.memPoints[0][0], self.memPoints[0][1], "lime")
                    a = polygon_area(self.memPoints)

                    if self.FFmpegGlobalFrame in self.measurement_w.draw_mem:
                        self.measurement_w.draw_mem[self.FFmpegGlobalFrame].append(["polygon", self.memPoints])
                    else:
                        self.measurement_w.draw_mem[self.FFmpegGlobalFrame] = [["polygon", self.memPoints]]
                    try:
                        a = a / (float(self.measurement_w.lePx.text())**2) * float(self.measurement_w.leRef.text())**2
                    except:
                        QMessageBox.critical(self, programName, """Check reference and pixel values! Values must be numeric.""", QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)

                    self.measurement_w.pte.appendPlainText("Time: {}\tFrame: {}\tArea: {}".format(self.getLaps(),
                                                                                                     self.FFmpegGlobalFrame,
                                                                                                     round(a, 1)))

                    self.memPoints = []


    def initialize_video_tab(self):
        # creating a basic vlc instance
        self.instance = vlc.Instance()

        # creating an empty vlc media player
        self.mediaplayer = self.instance.media_player_new()
        self.mediaListPlayer = self.instance.media_list_player_new()
        self.mediaListPlayer.set_media_player(self.mediaplayer)

        self.media_list = self.instance.media_list_new()

        # video will be drawn in this widget
        if sys.platform == "darwin":  # for MacOS
            self.videoframe = QMacCocoaViewContainer(0)
        else:
            self.videoframe = QFrame()
        self.palette = self.videoframe.palette()
        self.palette.setColor (QPalette.Window, QColor(0, 0, 0))
        self.videoframe.setPalette(self.palette)
        self.videoframe.setAutoFillBackground(True)

        self.volumeslider = QSlider(QtCore.Qt.Vertical, self)
        self.volumeslider.setMaximum(100)
        self.volumeslider.setValue(self.mediaplayer.audio_get_volume())
        self.volumeslider.setToolTip("Volume")
        self.volumeslider.sliderMoved.connect(self.setVolume)

        self.hsVideo = QSlider(QtCore.Qt.Horizontal, self)
        self.hsVideo.setMaximum(slider_maximum)
        self.hsVideo.sliderMoved.connect(self.hsVideo_sliderMoved)

        self.video1layout = QHBoxLayout()
        self.video1layout.addWidget(self.videoframe)
        self.video1layout.addWidget(self.volumeslider)

        self.vboxlayout = QVBoxLayout()

        self.vboxlayout.addLayout(self.video1layout)

        self.vboxlayout.addWidget(self.hsVideo)
        self.hsVideo.setVisible(True)

        self.videoTab = QWidget()

        self.videoTab.setLayout(self.vboxlayout)

        self.toolBox.insertItem(VIDEO_TAB, self.videoTab, "Audio/Video")

        self.actionFrame_by_frame.setEnabled(False)


        self.ffmpegLayout = QHBoxLayout()
        self.lbFFmpeg = QLabel(self)
        self.lbFFmpeg.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.lbFFmpeg.setBackgroundRole(QPalette.Base)
        self.lbFFmpeg.mousePressEvent = self.getPoslbFFmpeg

        self.ffmpegLayout.addWidget(self.lbFFmpeg)

        self.ffmpegTab = QWidget()
        self.ffmpegTab.setLayout(self.ffmpegLayout)

        self.toolBox.insertItem(FRAME_TAB, self.ffmpegTab, "Frame by frame")
        self.toolBox.setItemEnabled (FRAME_TAB, False)

        self.actionFrame_by_frame.setEnabled(True)

    def initialize_2nd_video_tab(self):
        """
        initialize second video player (use only if first player initialized)
        """
        self.mediaplayer2 = self.instance.media_player_new()

        self.media_list2 = self.instance.media_list_new()

        self.mediaListPlayer2 = self.instance.media_list_player_new()
        self.mediaListPlayer2.set_media_player(self.mediaplayer2)

        app.processEvents()

        if sys.platform == "darwin":  # for MacOS
            self.videoframe2 = QMacCocoaViewContainer(0)
        else:
            self.videoframe2 = QFrame()
        self.palette2 = self.videoframe2.palette()
        self.palette2.setColor(QPalette.Window, QColor(0, 0, 0))
        self.videoframe2.setPalette(self.palette2)
        self.videoframe2.setAutoFillBackground(True)

        self.volumeslider2 = QSlider(QtCore.Qt.Vertical, self)
        self.volumeslider2.setMaximum(100)
        self.volumeslider2.setValue(self.mediaplayer2.audio_get_volume())
        self.volumeslider2.setToolTip("Volume")

        self.volumeslider2.sliderMoved.connect(self.setVolume2)

        self.video2layout = QHBoxLayout()
        self.video2layout.addWidget(self.videoframe2)
        self.video2layout.addWidget(self.volumeslider2)

        self.vboxlayout.insertLayout(1, self.video2layout)


    def check_if_media_available(self):
        """
        check if every media available for observationId
        """

        if PLAYER1 not in self.pj[OBSERVATIONS][self.observationId][FILE]:
            return False

        if type(self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER1]) != type([]):
            return False

        if not self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER1]:
            return False

        for mediaFile in self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER1]:
            if not os.path.isfile(mediaFile):
                return False

        if PLAYER2 in self.pj[OBSERVATIONS][self.observationId][FILE]:

            if type(self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER2]) != type([]):
                return False

            if self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER2]:
                for mediaFile in self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER2]:
                    if not os.path.isfile(mediaFile):
                        return False
        return True

    def check_if_media_in_project_directory(self):

        try:
            for player in [PLAYER1, PLAYER2]:
                for mediaFile in self.pj[OBSERVATIONS][self.observationId][FILE][player]:
                    if not os.path.isfile(os.path.dirname(self.projectFileName) + os.sep + os.path.basename(mediaFile)):
                        return False
        except:
            return False
        return True


    def initialize_new_observation_vlc(self):
        """
        initialize new observation for VLC
        """

        logging.debug('initialize new observation for VLC')

        useMediaFromProjectDirectory = NO

        if not self.check_if_media_available():

            if self.check_if_media_in_project_directory():

                useMediaFromProjectDirectory = dialog.MessageDialog(programName, """Media file was/were not found in its/their original path(s) but in project directory.<br>
                Do you want to convert media file paths?""", [YES, NO])

                if useMediaFromProjectDirectory == NO:
                    QMessageBox.warning(self, programName, """The observation will be opened in VIEW mode.<br>
                    It will not be allowed to log events.<br>Modify the media path to point an existing media file to log events or copy media file in the BORIS project directory.""",
                    QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)

                    self.playerType = VIEWER
                    self.playMode = ""
                    self.dwObservations.setVisible(True)
                    return True

            else:
                QMessageBox.critical(self, programName, """A media file was not found!<br>The observation will be opened in VIEW mode.<br>
                It will not be allowed to log events.<br>Modify the media path to point an existing media file to log events or copy media file in the BORIS project directory.""",
                QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)

                self.playerType = VIEWER
                self.playMode = ''
                self.dwObservations.setVisible(True)
                return True

        # check if media list player 1 contains more than 1 media
        if (len(self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER1]) > 1
            and PLAYER2 in self.pj[OBSERVATIONS][self.observationId][FILE]
            and self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER2]):
               QMessageBox.warning(self, programName, "It is not yet possible to play a second media when more media are loaded in the first media player")
               return False

        self.playerType = VLC
        self.playMode = VLC
        self.fps = {}
        self.toolBar.setEnabled(False)
        self.dwObservations.setVisible(True)
        self.toolBox.setVisible(True)
        self.lbFocalSubject.setVisible(True)
        self.lbCurrentStates.setVisible(True)

        # init duration of media file and FPS
        self.duration.clear()
        self.duration2.clear()
        self.fps.clear()
        self.fps2.clear()

        # add all media files to media list
        self.simultaneousMedia = False

        if useMediaFromProjectDirectory == YES:
            for idx, mediaFile in enumerate(self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER1]):
                self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER1][idx] = os.path.dirname(self.projectFileName) + os.sep + os.path.basename(mediaFile)
                self.projectChanged = True

        for mediaFile in self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER1]:
            logging.debug("media file: {}".format(mediaFile))
            try:
                self.instance
            except AttributeError:
                self.initialize_video_tab()

            media = self.instance.media_new(mediaFile)
            media.parse()

            # media duration
            try:
                mediaLength = self.pj[OBSERVATIONS][self.observationId]["media_info"]["length"][mediaFile] * 1000
                mediaFPS = self.pj[OBSERVATIONS][self.observationId]["media_info"]["fps"][mediaFile]
            except:
                logging.debug("media_info key not found")
                nframe, videoTime, videoDuration, fps, hasVideo, hasAudio = accurate_media_analysis(self.ffmpeg_bin, mediaFile)
                if "media_info" not in self.pj[OBSERVATIONS][self.observationId]:
                    self.pj[OBSERVATIONS][self.observationId]["media_info"] = {"length": {}, "fps": {}}
                    if "length" not in self.pj[OBSERVATIONS][self.observationId]["media_info"]:
                        self.pj[OBSERVATIONS][self.observationId]["media_info"]["length"] = {}
                    if "fps" not in self.pj[OBSERVATIONS][self.observationId]["media_info"]:
                        self.pj[OBSERVATIONS][self.observationId]["media_info"]["fps"] = {}

                self.pj[OBSERVATIONS][self.observationId]["media_info"]["length"][mediaFile] = videoDuration
                self.pj[OBSERVATIONS][self.observationId]["media_info"]["fps"][mediaFile] = fps

                mediaLength = videoDuration * 1000
                mediaFPS = fps

                self.projectChanged = True

            self.duration.append(int(mediaLength))
            self.fps[mediaFile] = mediaFPS
            self.media_list.add_media(media)

        # add media list to media player list
        self.mediaListPlayer.set_media_list(self.media_list)

        # display media player in videoframe
        if self.embedPlayer:

            if sys.platform.startswith("linux"):  # for Linux using the X Server
                self.mediaplayer.set_xwindow(self.videoframe.winId())

            elif sys.platform.startswith("win"):  # for Windows
                self.mediaplayer.set_hwnd(int(self.videoframe.winId()))

        # for mac always embed player
        if sys.platform == "darwin":  # for MacOS
            self.mediaplayer.set_nsobject(int(self.videoframe.winId()))

        # check if fps changes between media
        """
        TODO: check
        if FFMPEG in self.availablePlayers:
            if len(set( self.fps.values() )) != 1:
                QMessageBox.critical(self, programName, "The frame-by-frame mode will not be available because the video files have different frame rates (%s)." % (", ".join([str(i) for i in list(self.fps.values())])),\
                 QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)
        """

        # show first frame of video
        logging.debug("playing media #{0}".format(0))

        self.mediaListPlayer.play_item_at_index(0)
        #app.processEvents()

        # play mediaListPlayer for a while to obtain media information
        while True:
            if self.mediaListPlayer.get_state() in [vlc.State.Playing, vlc.State.Ended]:
                break

        self.mediaListPlayer.pause()
        while True:
            if self.mediaListPlayer.get_state() in [vlc.State.Paused, vlc.State.Ended]:
                break
        #app.processEvents()
        self.mediaplayer.set_time(0)

        # no subtitles
        #self.mediaplayer.video_set_spu(0)

        self.FFmpegTimer = QTimer(self)
        self.FFmpegTimer.timeout.connect(self.ffmpegTimerOut)
        try:
            self.FFmpegTimerTick = int(1000 / list(self.fps.values())[0])
        except:
            self.FFmpegTimerTick = 40

        self.FFmpegTimer.setInterval(self.FFmpegTimerTick)

        # check for second media to be played together
        if PLAYER2 in self.pj[OBSERVATIONS][self.observationId][FILE] and self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER2]:

                if useMediaFromProjectDirectory == YES:
                    for idx, mediaFile in enumerate(self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER2]):
                        self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER2][idx] = os.path.dirname(self.projectFileName) +os.sep+ os.path.basename(mediaFile)
                        self.projectChanged = True

                # create 2nd mediaplayer
                self.simultaneousMedia = True
                self.initialize_2nd_video_tab()

                # add media file
                for mediaFile in self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER2]:
                    media = self.instance.media_new(mediaFile)
                    media.parse()

                    # media duration
                    try:
                        mediaLength = self.pj[OBSERVATIONS][self.observationId]["media_info"]["length"][mediaFile] * 1000
                        mediaFPS = self.pj[OBSERVATIONS][self.observationId]["media_info"]["fps"][mediaFile]
                    except:
                        logging.debug("media_info key not found")
                        nframe, videoTime, videoDuration, fps, hasVideo, hasAudio = accurate_media_analysis(self.ffmpeg_bin, mediaFile)
                        if "media_info" not in self.pj[OBSERVATIONS][self.observationId]:
                            self.pj[OBSERVATIONS][self.observationId]["media_info"] = {"length": {}, "fps": {}}
                            if "length" not in self.pj[OBSERVATIONS][self.observationId]["media_info"]:
                                self.pj[OBSERVATIONS][self.observationId]["media_info"]["length"] = {}
                            if "fps" not in self.pj[OBSERVATIONS][self.observationId]["media_info"]:
                                self.pj[OBSERVATIONS][self.observationId]["media_info"]["fps"] = {}

                        self.pj[OBSERVATIONS][self.observationId]["media_info"]["length"][mediaFile] = videoDuration
                        self.pj[OBSERVATIONS][self.observationId]["media_info"]["fps"][mediaFile] = fps

                        mediaLength = videoDuration * 1000
                        mediaFPS = fps
                        self.projectChanged = True

                    self.duration2.append(int(mediaLength))
                    self.fps2[mediaFile] = mediaFPS

                    self.media_list2.add_media(media)

                self.mediaListPlayer2.set_media_list(self.media_list2)

                if self.embedPlayer:
                    if sys.platform.startswith("linux"):  # for Linux using the X Server
                        self.mediaplayer2.set_xwindow(self.videoframe2.winId())

                    elif sys.platform.startswith("win32"):  # for Windows
                        self.mediaplayer2.set_hwnd(int(self.videoframe2.winId()) )

                        # self.mediaplayer.set_hwnd(self.videoframe.winId())

                # for mac always embed player
                if sys.platform == "darwin": # for MacOS
                    self.mediaplayer2.set_nsobject(int(self.videoframe2.winId()))

                # show first frame of video
                app.processEvents()

                self.mediaListPlayer2.play()
                app.processEvents()

                while True:
                    if self.mediaListPlayer2.get_state() in [vlc.State.Playing, vlc.State.Ended]:
                        break

                self.mediaListPlayer2.pause()
                app.processEvents()

                self.mediaplayer2.set_time(0)
                '''
                if TIME_OFFSET_SECOND_PLAYER in self.pj[OBSERVATIONS][self.observationId] \
                    and self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER]:
                    if self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] > 0:
                        self.mediaplayer2.set_time( int( self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] *1000) )
                '''

                # no subtitles
                #self.mediaplayer2.video_set_spu(0)


        self.videoTab.setEnabled(True)

        self.toolBox.setCurrentIndex(VIDEO_TAB)
        self.toolBox.setItemEnabled (VIDEO_TAB, False)

        self.toolBar.setEnabled(True)

        self.display_timeoffset_statubar(self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET])

        self.memMedia, self.currentSubject = "", ""

        self.timer_out()

        self.lbSpeed.setText("x{:.3f}".format(self.play_rate))

        if window.focusWidget():
            window.focusWidget().installEventFilter(self)

        '''
        if app.focusWidget():
            app.focusWidget().installEventFilter(self)
        '''

        # spectrogram
        if "visualize_spectrogram" in self.pj[OBSERVATIONS][self.observationId] and self.pj[OBSERVATIONS][self.observationId]["visualize_spectrogram"]:

            #self.memChunk = ''
            if not self.ffmpeg_cache_dir:
                tmp_dir = tempfile.gettempdir()
            else:
                tmp_dir = self.ffmpeg_cache_dir

            currentMediaTmpPath = tmp_dir + os.sep + os.path.basename(urllib.parse.unquote(url2path(self.mediaplayer.get_media().get_mrl())))

            if not os.path.isfile("{}.wav.0-{}.{}.{}.spectrogram.png".format(currentMediaTmpPath, self.chunk_length, self.spectrogram_color_map, self.spectrogramHeight)):
                if dialog.MessageDialog(programName, ("Spectrogram file not found.\n"
                                                      "Do you want to generate it now?\n"
                                                      "Spectrogram generation can take some time for long media, be patient"), [YES, NO ]) == YES:

                    self.generate_spectrogram()
                else:
                    self.pj[OBSERVATIONS][self.observationId]["visualize_spectrogram"] = False
                    return True

            self.spectro = plot_spectrogram.Spectrogram("{}.wav.0-{}.{}.{}.spectrogram.png".format(currentMediaTmpPath, self.chunk_length, self.spectrogram_color_map, self.spectrogramHeight))
            # connect signal from spectrogram class to testsignal function to receive keypress events
            self.spectro.setWindowFlags(Qt.WindowStaysOnTopHint)
            self.spectro.sendEvent.connect(self.signal_from_spectrogram)
            self.spectro.show()
            self.timer_spectro.start()


        # check if "filtered behaviors"
        if FILTERED_BEHAVIORS in self.pj[OBSERVATIONS][self.observationId]:
            self.load_behaviors_in_twEthogram(self.pj[OBSERVATIONS][self.observationId][FILTERED_BEHAVIORS])

        return True

    def signal_from_spectrogram(self, event):
        """
        receive signal from spectrogram widget
        """
        self.keyPressEvent(event)


    def eventFilter(self, source, event):
        """
        send event from widget to mainwindow
        """

        if event.type() == QtCore.QEvent.KeyPress:
            key = event.key()
            if key in [Qt.Key_Up, Qt.Key_Down, Qt.Key_Left, Qt.Key_Right, Qt.Key_PageDown, Qt.Key_PageUp]:
                self.keyPressEvent(event)

        return QMainWindow.eventFilter(self, source, event)


    def loadEventsInTW(self, obsId):
        """
        load events in table widget and update START/STOP
        """

        self.twEvents.setRowCount(len(self.pj[OBSERVATIONS][obsId][EVENTS]))
        row = 0

        for event in self.pj[OBSERVATIONS][obsId][EVENTS]:

            for field_type in tw_events_fields:

                if field_type in pj_events_fields:

                    field = event[pj_obs_fields[field_type]]
                    if field_type == "time":
                        field = str( self.convertTime( field) )

                    twi = QTableWidgetItem(field )
                    self.twEvents.setItem(row, tw_obs_fields[field_type], twi)

                else:
                    self.twEvents.setItem(row, tw_obs_fields[field_type], QTableWidgetItem(""))

            row += 1

        self.update_events_start_stop()


    def selectObservations(self, mode):
        """
        show observations list window
        mode: accepted values: OPEN, EDIT, SINGLE, MULTIPLE, SELECT1
        """

        obsListFields = ["id", "date", "description", "subjects", "media"]

        indepVarHeader, column_type = [], [TEXT] * len(obsListFields)

        '''
        NUMERIC = "numeric"
        NUMERIC_idx = 0
        TEXT = "text"
        TEXT_idx = 1
        SET_OF_VALUES = "value from set"
        SET_OF_VALUES_idx = 2
        '''


        if INDEPENDENT_VARIABLES in self.pj:
            for idx in [str(x) for x in sorted([int(x) for x in self.pj[INDEPENDENT_VARIABLES].keys()])]:
                indepVarHeader.append(self.pj[INDEPENDENT_VARIABLES][idx]["label"])
                column_type.append(self.pj[INDEPENDENT_VARIABLES][idx]["type"])


        data = []
        for obs in sorted(list(self.pj[OBSERVATIONS].keys())):
            date = self.pj[OBSERVATIONS][obs]["date"].replace("T", " ")
            descr = self.pj[OBSERVATIONS][obs]["description"]

            # subjects
            observedSubjects = self.extract_observed_subjects([obs])

            # remove when No focal subject
            if "" in observedSubjects:
                observedSubjects.remove("")
            subjectsList = ", ".join(observedSubjects)

            mediaList = []
            if self.pj[OBSERVATIONS][obs][TYPE] in [MEDIA]:
                if self.pj[OBSERVATIONS][obs][FILE]:
                    for player in sorted(self.pj[OBSERVATIONS][obs][FILE].keys()):
                        for media in self.pj[OBSERVATIONS][obs][FILE][player]:
                            mediaList.append("#{0}: {1}".format(player, media))

                media = os.linesep.join(mediaList)
            elif self.pj[OBSERVATIONS][obs][TYPE] in [LIVE]:
                media = LIVE

            # independent variables
            indepvar = []
            if INDEPENDENT_VARIABLES in self.pj[OBSERVATIONS][obs]:
                for var_label in indepVarHeader:
                    if var_label in self.pj[OBSERVATIONS][obs][INDEPENDENT_VARIABLES]:
                        indepvar.append(self.pj[OBSERVATIONS][obs][INDEPENDENT_VARIABLES][var_label])
                    else:
                        indepvar.append("")



            data.append([obs, date, descr, subjectsList, media] + indepvar)

        obsList = observations_list.observationsList_widget(data, header=obsListFields + indepVarHeader, column_type=column_type)

        obsList.pbOpen.setVisible(False)
        obsList.pbEdit.setVisible(False)
        obsList.pbOk.setVisible(False)
        obsList.pbSelectAll.setVisible(False)
        obsList.pbUnSelectAll.setVisible(False)
        obsList.mode = mode

        if mode == OPEN:
            obsList.view.setSelectionMode( QAbstractItemView.SingleSelection )
            obsList.pbOpen.setVisible(True)

        if mode == EDIT:
            obsList.view.setSelectionMode( QAbstractItemView.SingleSelection )
            obsList.pbEdit.setVisible(True)

        if mode == SINGLE:
            obsList.view.setSelectionMode( QAbstractItemView.SingleSelection )
            obsList.pbOpen.setVisible(True)
            obsList.pbEdit.setVisible(True)

        if mode == MULTIPLE:
            obsList.view.setSelectionMode( QAbstractItemView.MultiSelection )
            obsList.pbOk.setVisible(True)
            obsList.pbSelectAll.setVisible(True)
            obsList.pbUnSelectAll.setVisible(True)

        if mode == SELECT1:
            obsList.view.setSelectionMode( QAbstractItemView.SingleSelection )
            obsList.pbOk.setVisible(True)

        #obsList.comboBox.addItems(obsListFields + indepVarHeader)

        #obsList.view.setHorizontalHeaderLabels(obsListFields + indepVarHeader)
        #obsList.view.setHorizontalHeaderLabels(obsListFields + indepVarHeader)

        #obsList.view.horizontalHeader().setStretchLastSection(True)
        #obsList.view.resizeColumnsToContents()

        #obsList.view.setEditTriggers(QAbstractItemView.NoEditTriggers);
        #obsList.label.setText("{} observation{}".format(obsList.view.rowCount(), "s" * (obsList.view.rowCount()>1)))

        obsList.resize(900, 600)

        # sort memory
        '''
        iniFilePath = os.path.expanduser("~") + os.sep + ".boris"
        settings = QSettings(iniFilePath, QSettings.IniFormat)
        try:
            obsList.view.sortItems(settings.value("observations_list_order"), )
        except:
            print("Error:", sys.exc_info()[0])
        '''

        obsList.view.sortItems(0, Qt.AscendingOrder)

        selectedObs = []

        result = obsList.exec_()

        if result:
            if obsList.view.selectedIndexes():
                for idx in obsList.view.selectedIndexes():
                    if idx.column() == 0:   # first column
                        selectedObs.append(idx.data())

        if result == 0:  # cancel
            resultStr = ""
        if result == 1:   # select
            resultStr = "ok"
        if result == 2:   # open
            resultStr = OPEN
        if result == 3:   # edit
            resultStr = EDIT

        return resultStr, selectedObs


    def initialize_new_live_observation(self):
        """
        initialize new live observation
        """

        self.playerType = LIVE
        self.playMode = LIVE

        self.create_live_tab()

        self.toolBox.setVisible(True)

        self.dwObservations.setVisible(True)

        self.simultaneousMedia = False

        self.lbFocalSubject.setVisible(True)
        self.lbCurrentStates.setVisible(True)


        self.liveTab.setEnabled(True)
        self.toolBox.setItemEnabled (0, True)   # enable tab
        self.toolBox.setCurrentIndex(0)  # show tab

        self.toolBar.setEnabled(False)

        self.liveObservationStarted = False
        self.textButton.setText("Start live observation")
        if self.timeFormat == HHMMSS:
            self.lbTimeLive.setText("00:00:00.000")
        if self.timeFormat == S:
            self.lbTimeLive.setText("0.000")

        self.liveStartTime = None
        self.liveTimer.stop()

    def new_observation_triggered(self):
        self.new_observation(mode=NEW, obsId="")


    def new_observation(self, mode=NEW, obsId=""):
        """
        define a new observation or edit an existing observation
        """
        # check if current observation must be closed to create a new one
        if mode == NEW and self.observationId:
            if dialog.MessageDialog(programName, "The current observation will be closed. Do you want to continue?", [YES, NO]) == NO:
                return
            else:
                self.close_observation()

        observationWindow = observation.Observation(logging.getLogger().getEffectiveLevel())

        observationWindow.pj = self.pj
        observationWindow.mode = mode
        observationWindow.mem_obs_id = obsId
        observationWindow.chunk_length = self.chunk_length
        observationWindow.ffmpeg_cache_dir = self.ffmpeg_cache_dir
        observationWindow.dteDate.setDateTime(QDateTime.currentDateTime())
        observationWindow.FLAG_MATPLOTLIB_INSTALLED = FLAG_MATPLOTLIB_INSTALLED
        observationWindow.ffmpeg_bin = self.ffmpeg_bin
        observationWindow.spectrogramHeight = self.spectrogramHeight
        observationWindow.spectrogram_color_map = self.spectrogram_color_map

        # add indepvariables
        if INDEPENDENT_VARIABLES in self.pj:

            observationWindow.twIndepVariables.setRowCount(0)
            for i in [str(x) for x in sorted([int(x) for x in self.pj[INDEPENDENT_VARIABLES].keys()])]:

                observationWindow.twIndepVariables.setRowCount(observationWindow.twIndepVariables.rowCount() + 1)

                # label
                item = QTableWidgetItem()
                indepVarLabel = self.pj[INDEPENDENT_VARIABLES][i]['label']
                item.setText( indepVarLabel )
                item.setFlags(Qt.ItemIsEnabled)
                observationWindow.twIndepVariables.setItem(observationWindow.twIndepVariables.rowCount() - 1, 0, item)

                # var type
                item = QTableWidgetItem()
                item.setText( self.pj[INDEPENDENT_VARIABLES][i]["type"])
                item.setFlags(Qt.ItemIsEnabled)   # not modifiable
                observationWindow.twIndepVariables.setItem(observationWindow.twIndepVariables.rowCount() - 1, 1, item)

                # var value
                item = QTableWidgetItem()
                # check if obs has independent variables and var label is a key
                if mode == EDIT and INDEPENDENT_VARIABLES in self.pj[OBSERVATIONS][obsId] and indepVarLabel in self.pj[OBSERVATIONS][obsId][INDEPENDENT_VARIABLES]:
                    txt = self.pj[OBSERVATIONS][obsId][INDEPENDENT_VARIABLES][indepVarLabel]

                elif mode == NEW:
                    txt = self.pj[INDEPENDENT_VARIABLES][i]["default value"]
                else:
                    txt = ""

                if self.pj[INDEPENDENT_VARIABLES][i]["type"] == SET_OF_VALUES:
                    comboBox = QComboBox()
                    comboBox.addItems(self.pj[INDEPENDENT_VARIABLES][i]["possible values"].split(","))
                    if txt in self.pj[INDEPENDENT_VARIABLES][i]["possible values"].split(","):
                        comboBox.setCurrentIndex(self.pj[INDEPENDENT_VARIABLES][i]["possible values"].split(",").index(txt))
                    observationWindow.twIndepVariables.setCellWidget(observationWindow.twIndepVariables.rowCount() - 1, 2, comboBox)
                else:
                    item.setText(txt)
                    observationWindow.twIndepVariables.setItem(observationWindow.twIndepVariables.rowCount() - 1, 2, item)


            observationWindow.twIndepVariables.resizeColumnsToContents()

        # adapt time offset for current time format
        if self.timeFormat == S:
            observationWindow.teTimeOffset.setVisible(False)
            observationWindow.teTimeOffset_2.setVisible(False)

        if self.timeFormat == HHMMSS:
            observationWindow.leTimeOffset.setVisible(False)
            observationWindow.leTimeOffset_2.setVisible(False)

        if mode == EDIT:

            observationWindow.setWindowTitle("""Edit observation "{}" """.format(obsId))
            mem_obs_id = obsId
            observationWindow.leObservationId.setText(obsId)
            observationWindow.dteDate.setDateTime( QDateTime.fromString( self.pj[OBSERVATIONS][obsId]["date"], "yyyy-MM-ddThh:mm:ss") )
            observationWindow.teDescription.setPlainText( self.pj[OBSERVATIONS][obsId]["description"] )

            try:
                observationWindow.mediaDurations = self.pj[OBSERVATIONS][obsId]["media_info"]["length"]
                observationWindow.mediaFPS = self.pj[OBSERVATIONS][obsId]["media_info"]["fps"]
            except:
                observationWindow.mediaDurations = {}
                observationWindow.mediaFPS = {}


            try:
                if "hasVideo" in self.pj[OBSERVATIONS][obsId]["media_info"]:
                    observationWindow.mediaHasVideo = self.pj[OBSERVATIONS][obsId]["media_info"]["hasVideo"]
                if "hasAudio" in self.pj[OBSERVATIONS][obsId]["media_info"]:
                    observationWindow.mediaHasAudio = self.pj[OBSERVATIONS][obsId]["media_info"]["hasAudio"]
            except:
                logging.info("No Video/Audio information")

            # offset
            if self.timeFormat == S:

                observationWindow.leTimeOffset.setText( self.convertTime( abs(self.pj[OBSERVATIONS][obsId]["time offset"]) ))

                if "time offset second player" in self.pj[OBSERVATIONS][obsId]:
                    observationWindow.leTimeOffset_2.setText( self.convertTime( abs(self.pj[OBSERVATIONS][obsId]["time offset second player"]) ))

                    if self.pj[OBSERVATIONS][obsId]["time offset second player"] <= 0:
                        observationWindow.rbEarlier.setChecked(True)
                    else:
                        observationWindow.rbLater.setChecked(True)

            if self.timeFormat == HHMMSS:

                time = QTime()
                h,m,s_dec = seconds2time(abs(self.pj[OBSERVATIONS][obsId]["time offset"])).split(":")
                s, ms = s_dec.split(".")
                time.setHMS(int(h), int(m), int(s), int(ms))
                observationWindow.teTimeOffset.setTime(time)

                if "time offset second player" in self.pj[OBSERVATIONS][obsId]:
                    time = QTime()
                    h,m,s_dec = seconds2time(abs(self.pj[OBSERVATIONS][obsId]["time offset second player"])).split(':')
                    s, ms = s_dec.split(".")
                    time.setHMS(int(h), int(m), int(s), int(ms))
                    observationWindow.teTimeOffset_2.setTime(time)

                    if self.pj[OBSERVATIONS][obsId]["time offset second player"] <= 0:
                        observationWindow.rbEarlier.setChecked(True)
                    else:
                        observationWindow.rbLater.setChecked(True)


            if self.pj[OBSERVATIONS][obsId]["time offset"] < 0:
                observationWindow.rbSubstract.setChecked(True)

            for player, twVideo in zip([PLAYER1, PLAYER2], [observationWindow.twVideo1, observationWindow.twVideo2]):

                if player in self.pj[OBSERVATIONS][obsId][FILE] and self.pj[OBSERVATIONS][obsId][FILE][player]:
                    twVideo.setRowCount(0)
                    for mediaFile in self.pj[OBSERVATIONS][obsId][FILE] and self.pj[OBSERVATIONS][obsId][FILE][player]:
                        twVideo.setRowCount(twVideo.rowCount() + 1)
                        twVideo.setItem(twVideo.rowCount() - 1, 0, QTableWidgetItem(mediaFile))
                        try:
                            twVideo.setItem(twVideo.rowCount() - 1, 1, QTableWidgetItem(seconds2time(self.pj[OBSERVATIONS][obsId]["media_info"]["length"][mediaFile])))
                            twVideo.setItem(twVideo.rowCount() - 1, 2, QTableWidgetItem("{}".format(self.pj[OBSERVATIONS][obsId]["media_info"]["fps"][mediaFile])))
                        except:
                            pass
                        try:
                            twVideo.setItem(twVideo.rowCount() - 1, 3, QTableWidgetItem("{}".format(self.pj[OBSERVATIONS][obsId]["media_info"]["hasVideo"][mediaFile])))
                            twVideo.setItem(twVideo.rowCount() - 1, 4, QTableWidgetItem("{}".format(self.pj[OBSERVATIONS][obsId]["media_info"]["hasAudio"][mediaFile])))
                        except:
                            pass


            if self.pj[OBSERVATIONS][obsId]["type"] in [MEDIA]:
                observationWindow.tabProjectType.setCurrentIndex(video)

            if self.pj[OBSERVATIONS][obsId]["type"] in [LIVE]:
                observationWindow.tabProjectType.setCurrentIndex(live)
                if "scan_sampling_time" in self.pj[OBSERVATIONS][obsId]:
                    observationWindow.sbScanSampling.setValue(self.pj[OBSERVATIONS][obsId]["scan_sampling_time"])


             # spectrogram
            observationWindow.cbVisualizeSpectrogram.setEnabled(True)
            if "visualize_spectrogram" in self.pj[OBSERVATIONS][obsId]:
                observationWindow.cbVisualizeSpectrogram.setChecked(self.pj[OBSERVATIONS][obsId]["visualize_spectrogram"])

            # cbCloseCurrentBehaviorsBetweenVideo
            observationWindow.cbCloseCurrentBehaviorsBetweenVideo.setEnabled(True)
            if CLOSE_BEHAVIORS_BETWEEN_VIDEOS in self.pj[OBSERVATIONS][obsId]:
                observationWindow.cbCloseCurrentBehaviorsBetweenVideo.setChecked(self.pj[OBSERVATIONS][obsId][CLOSE_BEHAVIORS_BETWEEN_VIDEOS])

        # spectrogram
        #observationWindow.cbVisualizeSpectrogram.setEnabled(FLAG_MATPLOTLIB_INSTALLED)

        rv = observationWindow.exec_()

        print("rv", rv)

        if rv:

            self.projectChanged = True

            new_obs_id = observationWindow.leObservationId.text()

            if mode == NEW:
                self.observationId = new_obs_id
                self.pj[OBSERVATIONS][self.observationId] = {FILE: [], TYPE: "",  "date": "", "description": "", "time offset": 0, "events": []}

            # check if id changed
            if mode == EDIT and new_obs_id != obsId:

                logging.info("observation id {} changed in {}".format(obsId, new_obs_id))

                self.pj[OBSERVATIONS][new_obs_id] = self.pj[OBSERVATIONS][obsId]
                del self.pj[OBSERVATIONS][obsId]

            # observation date
            self.pj[OBSERVATIONS][new_obs_id]["date"] = observationWindow.dteDate.dateTime().toString(Qt.ISODate)

            self.pj[OBSERVATIONS][new_obs_id]["description"] = observationWindow.teDescription.toPlainText()

            # observation type: read project type from tab text
            self.pj[OBSERVATIONS][new_obs_id][TYPE] = observationWindow.tabProjectType.tabText( observationWindow.tabProjectType.currentIndex() ).upper()

            # independent variables for observation
            self.pj[OBSERVATIONS][new_obs_id][INDEPENDENT_VARIABLES] = {}
            for r in range(observationWindow.twIndepVariables.rowCount()):

                # set dictionary as label (col 0) => value (col 2)
                if observationWindow.twIndepVariables.item(r, 1).text() == SET_OF_VALUES:
                    self.pj[OBSERVATIONS][new_obs_id][INDEPENDENT_VARIABLES][observationWindow.twIndepVariables.item(r, 0).text()] = observationWindow.twIndepVariables.cellWidget(r, 2).currentText()
                else:
                    self.pj[OBSERVATIONS][new_obs_id][INDEPENDENT_VARIABLES][observationWindow.twIndepVariables.item(r, 0).text()] = observationWindow.twIndepVariables.item(r, 2).text()

            # observation time offset
            if self.timeFormat == HHMMSS:
                self.pj[OBSERVATIONS][new_obs_id][TIME_OFFSET] = time2seconds(observationWindow.teTimeOffset.time().toString(HHMMSSZZZ))
                self.pj[OBSERVATIONS][new_obs_id][TIME_OFFSET_SECOND_PLAYER] = time2seconds(observationWindow.teTimeOffset_2.time().toString(HHMMSSZZZ))

            if self.timeFormat == S:
                self.pj[OBSERVATIONS][new_obs_id][TIME_OFFSET] = abs(Decimal( observationWindow.leTimeOffset.text() ))
                self.pj[OBSERVATIONS][new_obs_id][TIME_OFFSET_SECOND_PLAYER] = abs(Decimal( observationWindow.leTimeOffset_2.text() ))

            if observationWindow.rbSubstract.isChecked():
                self.pj[OBSERVATIONS][new_obs_id][TIME_OFFSET] = - self.pj[OBSERVATIONS][new_obs_id][TIME_OFFSET]

            if observationWindow.rbEarlier.isChecked():
                self.pj[OBSERVATIONS][new_obs_id][TIME_OFFSET_SECOND_PLAYER] = - self.pj[OBSERVATIONS][new_obs_id][TIME_OFFSET_SECOND_PLAYER]


            self.display_timeoffset_statubar(self.pj[OBSERVATIONS][new_obs_id][TIME_OFFSET])

            # visualize spectrogram
            self.pj[OBSERVATIONS][new_obs_id]["visualize_spectrogram"] = observationWindow.cbVisualizeSpectrogram.isChecked()

            # cbCloseCurrentBehaviorsBetweenVideo
            self.pj[OBSERVATIONS][new_obs_id][CLOSE_BEHAVIORS_BETWEEN_VIDEOS] = observationWindow.cbCloseCurrentBehaviorsBetweenVideo.isChecked()

            if self.pj[OBSERVATIONS][new_obs_id][TYPE] in [LIVE]:
                self.pj[OBSERVATIONS][new_obs_id]["scan_sampling_time"] = observationWindow.sbScanSampling.value()

            # media file
            fileName = {}

            # media
            if self.pj[OBSERVATIONS][new_obs_id][TYPE] in [MEDIA]:

                fileName[PLAYER1] = []
                if observationWindow.twVideo1.rowCount():

                    for row in range(observationWindow.twVideo1.rowCount()):
                        fileName[PLAYER1].append(observationWindow.twVideo1.item(row, 0).text())

                fileName[PLAYER2] = []

                if observationWindow.twVideo2.rowCount():

                    for row in range(observationWindow.twVideo2.rowCount()):
                        fileName[PLAYER2].append(observationWindow.twVideo2.item(row, 0).text())

                self.pj[OBSERVATIONS][new_obs_id][FILE] = fileName

                self.pj[OBSERVATIONS][new_obs_id]["media_info"] = {"length": observationWindow.mediaDurations,
                                                                  "fps":  observationWindow.mediaFPS}

                try:
                    self.pj[OBSERVATIONS][new_obs_id]["media_info"]["hasVideo"] = observationWindow.mediaHasVideo
                    self.pj[OBSERVATIONS][new_obs_id]["media_info"]["hasAudio"] = observationWindow.mediaHasAudio
                except:
                    logging.info("error with media_info information")


                logging.debug("media_info: {0}".format(  self.pj[OBSERVATIONS][new_obs_id]['media_info'] ))

                '''
                if not 'project_media_file_info' in self.pj:
                    self.pj['project_media_file_info'] = {}


                for h in observationWindow.media_file_info:
                    self.pj['project_media_file_info'][h] = observationWindow.media_file_info[h]
                logging.info('pj: {0}'.format(  self.pj ))
                '''

            #if mode == NEW:

            if rv == 1: # save
                self.observationId = ""
                self.menu_options()

            if rv == 2:  # launch
                self.observationId = new_obs_id

                # title of dock widget
                self.dwObservations.setWindowTitle("""Events for "{}" observation""".format(self.observationId))

                if self.pj[OBSERVATIONS][self.observationId][TYPE] in [LIVE]:

                    self.playerType = LIVE
                    self.initialize_new_live_observation()

                else:
                    self.playerType = VLC

                    # load events in table widget
                    if mode == EDIT:
                        self.loadEventsInTW(self.observationId)

                    self.initialize_new_observation_vlc()

                self.menu_options()


    def close_tool_windows(self):
        """
        close tool windows: spectrogram, measurements, coding pad
        """

        try:
            self.measurement_w.close()
        except:
            pass

        if hasattr(self, "codingpad"):
            del self.codingpad

        if hasattr(self, "spectro"):
            del self.spectro

        if hasattr(self, "frame_viewer1"):
            del self.frame_viewer1

        if hasattr(self, "frame_viewer2"):
            del self.frame_viewer2

        if hasattr(self, "results"):
            del self.results


    def close_observation(self):
        """
        close current observation
        """

        logging.info("Close observation {}".format(self.playerType))

        self.observationId = ""

        self.close_tool_windows()

        if self.playerType == LIVE:

            self.liveObservationStarted = False
            self.liveStartTime = None
            self.liveTimer.stop()
            self.toolBox.removeItem(0)
            self.liveTab.deleteLater()

        if self.playerType == VLC:

            self.timer.stop()
            self.timer_spectro.stop()

            self.mediaplayer.stop()
            del self.mediaplayer
            del self.mediaListPlayer

            # empty media list
            while self.media_list.count():
                self.media_list.remove_index(0)

            del self.media_list

            while self.video1layout.count():
                item = self.video1layout.takeAt(0)
                item.widget().deleteLater()

            if self.simultaneousMedia:
                self.mediaplayer2.stop()
                while self.media_list2.count():
                    self.media_list2.remove_index(0)
                del self.mediaplayer2

                while self.video2layout.count():
                    item = self.video2layout.takeAt(0)
                    item.widget().deleteLater()

                self.simultaneousMedia = False

                del self.media_list2

            del self.instance

            self.videoTab.deleteLater()
            self.actionFrame_by_frame.setChecked(False)
            self.playMode = VLC

            try:
                self.spectro.close()
                del self.spectro
            except:
                pass

            try:
                self.ffmpegLayout.deleteLater()
                self.lbFFmpeg.deleteLater()
                self.ffmpegTab.deleteLater()

                self.FFmpegTimer.stop()
                self.FFmpegGlobalFrame = 0
                self.imagesList = set()
            except:
                pass

        self.statusbar.showMessage("", 0)

        # delete layout
        self.toolBar.setEnabled(False)
        self.dwObservations.setVisible(False)
        self.toolBox.setVisible(False)
        self.lbFocalSubject.setVisible(False)
        self.lbCurrentStates.setVisible(False)

        self.twEvents.setRowCount(0)

        self.lbTime.clear()
        self.lbSubject.clear()
        self.lbFocalSubject.setText(NO_FOCAL_SUBJECT)

        self.lbTimeOffset.clear()
        self.lbSpeed.clear()

        self.playerType = ""

        self.menu_options()



    def readConfigFile(self):
        """
        read config file
        """

        logging.info("read config file")

        if __version__ == 'DEV':
            iniFilePath = os.path.expanduser('~') + os.sep + '.boris_dev'
        else:
            iniFilePath = os.path.expanduser("~") + os.sep + ".boris"

        if os.path.isfile(iniFilePath):
            settings = QSettings(iniFilePath, QSettings.IniFormat)

            size = settings.value("MainWindow/Size")
            if size:
                self.resize(size)
                self.move(settings.value("MainWindow/Position"))

            self.timeFormat = HHMMSS
            try:
                self.timeFormat = settings.value("Time/Format")
            except:
                self.timeFormat = HHMMSS

            self.fast = 10
            try:
                self.fast = int(settings.value("Time/fast_forward_speed"))

            except:
                self.fast = 10

            self.repositioningTimeOffset = 0
            try:
                self.repositioningTimeOffset = int(settings.value("Time/Repositioning_time_offset"))

            except:
                self.repositioningTimeOffset = 0

            self.play_rate_step = 0.1
            try:
                self.play_rate_step = float(settings.value("Time/play_rate_step"))

            except:
                self.play_rate_step = 0.1

            #self.saveMediaFilePath = True

            self.automaticBackup = 0
            try:
                self.automaticBackup  = int(settings.value("Automatic_backup"))
            except:
                self.automaticBackup = 0

            self.behaviouralStringsSeparator = "|"
            try:
                self.behaviouralStringsSeparator = settings.value("behavioural_strings_separator")
                if not self.behaviouralStringsSeparator:
                    self.behaviouralStringsSeparator = "|"
            except:
                self.behaviouralStringsSeparator = "|"

            self.close_the_same_current_event = False
            try:
                self.close_the_same_current_event = (settings.value("close_the_same_current_event") == "true")
            except:
                self.close_the_same_current_event = False

            self.confirmSound = False
            try:
                self.confirmSound = (settings.value("confirm_sound") == "true")
            except:
                self.confirmSound = False

            self.embedPlayer = True
            try:
                self.embedPlayer = (settings.value("embed_player") == "true")
            except:
                self.embedPlayer = True

            self.alertNoFocalSubject = False
            try:
                self.alertNoFocalSubject = (settings.value('alert_nosubject') == "true")
            except:
                self.alertNoFocalSubject = False

            try:
                self.beep_every = int(settings.value("beep_every"))
            except:
                self.beep_every = 0

            self.trackingCursorAboveEvent = False
            try:
                self.trackingCursorAboveEvent = (settings.value('tracking_cursor_above_event') == "true")
            except:
                self.trackingCursorAboveEvent = False

            # check for new version
            self.checkForNewVersion = False
            try:
                if settings.value('check_for_new_version') == None:
                    self.checkForNewVersion = (dialog.MessageDialog(programName, ("Allow BORIS to automatically check for new version?\n"
                                                                          "(An internet connection is required)\n"
                                                                          "You can change this option in the Preferences (File > Preferences)"), [YES, NO ]) == YES)
                else:
                    self.checkForNewVersion = (settings.value('check_for_new_version') == 'true')
            except:
                self.checkForNewVersion = False

            if self.checkForNewVersion:
                if settings.value("last_check_for_new_version") and  int(time.mktime(time.localtime())) - int(settings.value('last_check_for_new_version')) > CHECK_NEW_VERSION_DELAY:
                    self.actionCheckUpdate_activated(flagMsgOnlyIfNew = True)

            self.ffmpeg_cache_dir = ""
            try:
                self.ffmpeg_cache_dir = settings.value("ffmpeg_cache_dir")
                if not self.ffmpeg_cache_dir:
                    self.ffmpeg_cache_dir = ""
            except:
                self.ffmpeg_cache_dir = ""

            self.ffmpeg_cache_dir_max_size = 0
            try:
                self.ffmpeg_cache_dir_max_size = int(settings.value("ffmpeg_cache_dir_max_size"))
                if not self.ffmpeg_cache_dir_max_size:
                    self.ffmpeg_cache_dir_max_size = 0
            except:
                self.ffmpeg_cache_dir_max_size = 0

            # frame-by-frame
            try:
                self.frame_resize = int(settings.value("frame_resize"))
                if not self.frame_resize:
                    self.frame_resize = 0
            except:
                self.frame_resize = 0

            try:
                self.frame_bitmap_format = settings.value("frame_bitmap_format")
                if not self.frame_bitmap_format:
                    self.frame_bitmap_format = FRAME_DEFAULT_BITMAT_FORMAT
            except:
                self.frame_bitmap_format = FRAME_DEFAULT_BITMAT_FORMAT


            try:
                self.detachFrameViewer = (settings.value("detach_frame_viewer") == "true")
            except:
                self.detachFrameViewer = False

            # spectrogram
            self.spectrogramHeight = 80
            try:
                self.spectrogramHeight = int(settings.value("spectrogram_height"))
                if not self.spectrogramHeight:
                    self.spectrogramHeight = 80
            except:
                self.spectrogramHeight = 80

            try:
                self.spectrogram_color_map = settings.value("spectrogram_color_map")
                if self.spectrogram_color_map is None:
                    self.spectrogram_color_map = SPECTROGRAM_DEFAULT_COLOR_MAP
            except:
                self.spectrogram_color_map = SPECTROGRAM_DEFAULT_COLOR_MAP


        else: # no .boris file found
            # ask user for checking for new version
            self.checkForNewVersion = (dialog.MessageDialog(programName, ("Allow BORIS to automatically check for new version?\n"
                                                                          "(An internet connection is required)\n"
                                                                          "You can change this option in the Preferences (File > Preferences)"), [NO, YES ]) == YES)

    def saveConfigFile(self, lastCheckForNewVersion=0):
        """
        save config file
        """

        logging.info("save config file")

        iniFilePath = os.path.expanduser("~") + os.sep + ".boris"

        settings = QSettings(iniFilePath, QSettings.IniFormat)
        settings.setValue("MainWindow/Size", self.size())
        settings.setValue("MainWindow/Position", self.pos())
        settings.setValue("Time/Format", self.timeFormat)
        settings.setValue("Time/Repositioning_time_offset", self.repositioningTimeOffset)
        settings.setValue("Time/fast_forward_speed", self.fast)
        settings.setValue("Time/play_rate_step", self.play_rate_step)
        settings.setValue("Save_media_file_path", self.saveMediaFilePath)
        settings.setValue("Automatic_backup", self.automaticBackup)
        settings.setValue("behavioural_strings_separator", self.behaviouralStringsSeparator)
        settings.setValue("close_the_same_current_event", self.close_the_same_current_event)
        settings.setValue("confirm_sound", self.confirmSound)
        settings.setValue("beep_every", self.beep_every)
        settings.setValue("embed_player", self.embedPlayer)
        settings.setValue("alert_nosubject", self.alertNoFocalSubject)
        settings.setValue("tracking_cursor_above_event", self.trackingCursorAboveEvent)
        settings.setValue("check_for_new_version", self.checkForNewVersion)
        if lastCheckForNewVersion:
            settings.setValue("last_check_for_new_version", lastCheckForNewVersion)

        # FFmpeg
        settings.setValue("ffmpeg_cache_dir", self.ffmpeg_cache_dir)
        settings.setValue("ffmpeg_cache_dir_max_size", self.ffmpeg_cache_dir_max_size)
        # frame-by-frame
        settings.setValue("frame_resize", self.frame_resize)

        settings.setValue("frame_bitmap_format",self.frame_bitmap_format)
        settings.setValue("detach_frame_viewer", self.detachFrameViewer)
        # spectrogram
        settings.setValue("spectrogram_height", self.spectrogramHeight)
        settings.setValue("spectrogram_color_map", self.spectrogram_color_map)



    def edit_project_activated(self):
        """
        edit project menu option triggered
        """
        if self.project:
            self.edit_project(EDIT)
        else:
            QMessageBox.warning(self, programName, "There is no project to edit")



    def display_timeoffset_statubar(self, timeOffset):
        """
        display offset in status bar
        """

        if timeOffset:
            self.lbTimeOffset.setText("Time offset: <b>{}</b>".format(timeOffset if self.timeFormat == S else seconds2time(timeOffset)))
        else:
            self.lbTimeOffset.clear()


    def eventType(self, code):
        """
        returns type of event for code
        """

        for idx in self.pj[ETHOGRAM]:
            if self.pj[ETHOGRAM][idx]['code'] == code:
                return self.pj[ETHOGRAM][idx][TYPE]
        return None


    def loadEventsInDB(self, selectedSubjects, selectedObservations, selectedBehaviors):
        """
        populate the db databse with events from selectedObservations, selectedSubjects and selectedBehaviors
        """
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row

        cursor = db.cursor()

        cursor.execute("CREATE TABLE events (observation TEXT, subject TEXT, code TEXT, type TEXT, modifiers TEXT, occurence FLOAT, comment TEXT);")

        for subject_to_analyze in selectedSubjects:

            for obsId in selectedObservations:

                for event in self.pj[OBSERVATIONS][obsId][EVENTS]:

                    if event[2] in selectedBehaviors:

                        # extract time, code, modifier and comment ( time:0, subject:1, code:2, modifier:3, comment:4 )
                        if (subject_to_analyze == NO_FOCAL_SUBJECT and event[1] == "") \
                            or ( event[1] == subject_to_analyze ):

                            subjectStr = NO_FOCAL_SUBJECT if event[1] == "" else  event[1]

                            eventType = STATE if STATE in self.eventType(event[2]).upper() else POINT

                            r = cursor.execute("""INSERT INTO events (observation, subject, code, type, modifiers, occurence, comment) VALUES (?,?,?,?,?,?,?)""",
                            (obsId, subjectStr, event[2], eventType, event[3], str(event[0]), event[4]))

        db.commit()
        return cursor


    def extract_observed_subjects(self, selected_observations):
        """
        extract unique subjects from obs_id observation
        """

        observed_subjects = []

        # extract events from selected observations
        all_events =   [ self.pj[OBSERVATIONS][x][EVENTS] for x in self.pj[OBSERVATIONS] if x in selected_observations]
        for events in all_events:
            for event in events:
                observed_subjects.append( event[pj_obs_fields['subject']] )

        # remove duplicate
        observed_subjects = list( set( observed_subjects ) )

        return observed_subjects


    def extract_observed_behaviors(self, selected_observations, selectedSubjects):
        """
        extract unique behaviors codes from obs_id observation
        """

        observed_behaviors = []

        # extract events from selected observations
        all_events = [self.pj[OBSERVATIONS][x][EVENTS] for x in self.pj[OBSERVATIONS] if x in selected_observations]

        for events in all_events:
            for event in events:
                if event[EVENT_SUBJECT_FIELD_IDX] in selectedSubjects or (not event[EVENT_SUBJECT_FIELD_IDX] and NO_FOCAL_SUBJECT in selectedSubjects):
                    observed_behaviors.append(event[EVENT_BEHAVIOR_FIELD_IDX])

        # remove duplicate
        observed_behaviors = list(set(observed_behaviors))

        return observed_behaviors


    def choose_obs_subj_behav_category(self, selectedObservations, maxTime, flagShowIncludeModifiers=True, flagShowExcludeBehaviorsWoEvents=True, by_category=False):
        """
        show window for:
        - selection of subjects
        - selection of behaviors (based on selected subjects)
        - selection of time interval
        - inclusion of modifiers
        - exclusion of behaviors without events (flagShowExcludeBehaviorsWoEvents == True)

        """

        paramPanelWindow = param_panel.Param_panel()
        paramPanelWindow.setWindowTitle("Select subjects and behaviors")
        paramPanelWindow.selectedObservations = selectedObservations
        paramPanelWindow.pj = self.pj
        paramPanelWindow.extract_observed_behaviors = self.extract_observed_behaviors

        if not flagShowIncludeModifiers:
            paramPanelWindow.cbIncludeModifiers.setVisible(False)
        if not flagShowExcludeBehaviorsWoEvents:
            paramPanelWindow.cbExcludeBehaviors.setVisible(False)

        if by_category:
            paramPanelWindow.cbIncludeModifiers.setVisible(False)
            paramPanelWindow.cbExcludeBehaviors.setVisible(False)

        # hide max time
        if maxTime:
            if self.timeFormat == HHMMSS:
                paramPanelWindow.teStartTime.setTime(QtCore.QTime.fromString("00:00:00.000", "hh:mm:ss.zzz"))
                paramPanelWindow.teEndTime.setTime(QtCore.QTime.fromString(seconds2time(maxTime), "hh:mm:ss.zzz"))
                paramPanelWindow.dsbStartTime.setVisible(False)
                paramPanelWindow.dsbEndTime.setVisible(False)

            if self.timeFormat == S:
                paramPanelWindow.dsbStartTime.setValue(0.0)
                paramPanelWindow.dsbEndTime.setValue(maxTime)
                paramPanelWindow.teStartTime.setVisible(False)
                paramPanelWindow.teEndTime.setVisible(False)

        else:
            paramPanelWindow.lbStartTime.setVisible(False)
            paramPanelWindow.lbEndTime.setVisible(False)

            paramPanelWindow.teStartTime.setVisible(False)
            paramPanelWindow.teEndTime.setVisible(False)

            paramPanelWindow.dsbStartTime.setVisible(False)
            paramPanelWindow.dsbEndTime.setVisible(False)


        # extract subjects present in observations
        observedSubjects = self.extract_observed_subjects(selectedObservations)
        selectedSubjects = []

        # add 'No focal subject'
        if "" in observedSubjects:
            selectedSubjects.append(NO_FOCAL_SUBJECT)
            paramPanelWindow.item = QListWidgetItem(paramPanelWindow.lwSubjects)
            paramPanelWindow.ch = QCheckBox()
            paramPanelWindow.ch.setText(NO_FOCAL_SUBJECT)
            paramPanelWindow.ch.stateChanged.connect(paramPanelWindow.cb_changed)
            paramPanelWindow.ch.setChecked(True)
            paramPanelWindow.lwSubjects.setItemWidget(paramPanelWindow.item, paramPanelWindow.ch)

        all_subjects = [self.pj[SUBJECTS][x]["name"] for x in sorted_keys(self.pj[SUBJECTS])]

        for subject in all_subjects:
            paramPanelWindow.item = QListWidgetItem(paramPanelWindow.lwSubjects)
            paramPanelWindow.ch = QCheckBox()
            paramPanelWindow.ch.setText( subject )
            paramPanelWindow.ch.stateChanged.connect(paramPanelWindow.cb_changed)
            if subject in observedSubjects:
                selectedSubjects.append(subject)
                paramPanelWindow.ch.setChecked(True)

            paramPanelWindow.lwSubjects.setItemWidget(paramPanelWindow.item, paramPanelWindow.ch)

        logging.debug('selectedSubjects: {0}'.format(selectedSubjects))

        observedBehaviors = self.extract_observed_behaviors(selectedObservations, selectedSubjects) # not sorted

        logging.debug('observed behaviors: {0}'.format(observedBehaviors))

        if BEHAVIORAL_CATEGORIES in self.pj:
            categories = self.pj[BEHAVIORAL_CATEGORIES][:]
            # check if behavior not included in a category
            try:
                if "" in [self.pj[ETHOGRAM][idx]["category"] for idx in self.pj[ETHOGRAM] if "category" in self.pj[ETHOGRAM][idx]]:
                    categories += [""]
            except:
                categories = ["###no category###"]

        else:
            categories = ["###no category###"]

        for category in categories:

            if category != "###no category###":
                if category == "":
                    paramPanelWindow.item = QListWidgetItem("No category")
                    paramPanelWindow.item.setData(34, "No category")
                else:
                    paramPanelWindow.item = QListWidgetItem(category)
                    paramPanelWindow.item.setData(34, category)

                font = QFont()
                font.setBold(True)
                #paramPanelWindow.item.setFont(QFont('', 8, QFont.Bold))
                paramPanelWindow.item.setFont(font)
                paramPanelWindow.item.setData(33, "category")
                paramPanelWindow.item.setData(35, False)

                paramPanelWindow.lwBehaviors.addItem(paramPanelWindow.item)

            for behavior in [self.pj[ETHOGRAM][x]["code"] for x in sorted_keys(self.pj[ETHOGRAM])]:

                if ((categories == ["###no category###"])
                or (behavior in [self.pj[ETHOGRAM][x]["code"] for x in self.pj[ETHOGRAM] if "category" in self.pj[ETHOGRAM][x] and self.pj[ETHOGRAM][x]["category"] == category])):

                    paramPanelWindow.item = QListWidgetItem(behavior)
                    if behavior in observedBehaviors:
                        paramPanelWindow.item.setCheckState(Qt.Checked)
                    else:
                        paramPanelWindow.item.setCheckState(Qt.Unchecked)

                    if category != "###no category###":
                        paramPanelWindow.item.setData(33, "behavior")
                        if category == "":
                            paramPanelWindow.item.setData(34, "No category")
                        else:
                            paramPanelWindow.item.setData(34, category)

                    paramPanelWindow.lwBehaviors.addItem(paramPanelWindow.item)


        if not paramPanelWindow.exec_():
            return {"selected subjects": [],
                    "selected behaviors": []}

        selectedSubjects = paramPanelWindow.selectedSubjects
        selectedBehaviors = paramPanelWindow.selectedBehaviors

        logging.debug("selected subjects: {}".format(selectedSubjects))
        logging.debug("selected behaviors: {}".format(selectedBehaviors))

        if self.timeFormat == HHMMSS:
            startTime = time2seconds(paramPanelWindow.teStartTime.time().toString(HHMMSSZZZ))
            endTime = time2seconds(paramPanelWindow.teEndTime.time().toString(HHMMSSZZZ))
        if self.timeFormat == S:
            startTime = Decimal(paramPanelWindow.dsbStartTime.value())
            endTime = Decimal(paramPanelWindow.dsbEndTime.value())
        if startTime > endTime:
            QMessageBox.warning(None, programName, "The start time is after the end time", QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)
            return {"selected subjects": [], "selected behaviors": []}


        return {"selected subjects": selectedSubjects,
                "selected behaviors": selectedBehaviors,
                "include modifiers": paramPanelWindow.cbIncludeModifiers.isChecked(),
                "exclude behaviors": paramPanelWindow.cbExcludeBehaviors.isChecked(),
                "start time": startTime,
                "end time": endTime
                }



    def time_budget_by_category(self, mode):
        """
        time budget (by behavior or category)
        """

        def time_budget_analysis_by_category(cursor, plot_parameters, by_category=False):

            categories = {}
            out = []

            for subject in plot_parameters["selected subjects"]:
                out_cat = []

                categories[subject] = {}

                print("selected behaviors", plot_parameters["selected behaviors"])
                for behavior in plot_parameters["selected behaviors"]:

                    if plot_parameters["include modifiers"]:

                        cursor.execute("SELECT distinct modifiers FROM events WHERE subject = ? AND code = ?", (subject, behavior))
                        distinct_modifiers = list(cursor.fetchall())

                        if not distinct_modifiers:
                            if not plot_parameters["exclude behaviors"]:
                                out.append({"subject": subject,
                                            "behavior": behavior,
                                            "modifiers": "-",
                                            "duration": "-",
                                            "duration_mean": "-",
                                            "duration_stdev": "-",
                                            "number": 0,
                                            "inter_duration_mean": "-",
                                            "inter_duration_stdev": "-"})
                            continue

                        if POINT in self.eventType(behavior).upper():
                            for modifier in distinct_modifiers:
                                if len(selectedObservations) > 1:
                                    cursor.execute("SELECT occurence,observation FROM events WHERE subject = ? AND code = ? AND modifiers = ? ORDER BY observation, occurence",
                                                   (subject, behavior, modifier[0]))
                                else:
                                    cursor.execute("SELECT occurence,observation FROM events WHERE subject = ? AND code = ? AND modifiers = ? AND occurence BETWEEN ? and ? ORDER BY observation, occurence",
                                                   (subject, behavior, modifier[0], str(plot_parameters["start time"]), str(plot_parameters["end time"])))

                                rows = cursor.fetchall()

                                # inter events duration
                                all_event_interdurations = []
                                for idx, row in enumerate(rows):
                                    if idx and row[1] == rows[idx - 1][1]:
                                        all_event_interdurations.append(float(row[0]) - float(rows[idx - 1][0]))

                                out_cat.append({"subject": subject,
                                            "behavior": behavior,
                                            "modifiers": modifier[0],
                                            "duration": "-",
                                            "duration_mean": "-",
                                            "duration_stdev": "-",
                                            "number": len(rows),
                                            "inter_duration_mean": round(statistics.mean(all_event_interdurations), 3) if len(all_event_interdurations) else "NA",
                                            "inter_duration_stdev": round(statistics.stdev(all_event_interdurations), 3) if len(all_event_interdurations) > 1 else "NA"
                                            })


                        if STATE in self.eventType(behavior).upper():
                            for modifier in distinct_modifiers:
                                cursor.execute("SELECT occurence,observation FROM events WHERE subject = ? AND code = ? AND modifiers = ? ORDER BY observation, occurence",
                                              (subject, behavior, modifier[0]))
                                rows = list(cursor.fetchall())
                                if len(rows) % 2:
                                    out.append({"subject": subject, "behavior": behavior,
                                                "modifiers": modifier[0], "duration": UNPAIRED,
                                                "duration_mean": UNPAIRED, "duration_stdev": UNPAIRED,
                                                "number": UNPAIRED, "inter_duration_mean": UNPAIRED,
                                                "inter_duration_stdev": UNPAIRED})
                                else:
                                    all_event_durations, all_event_interdurations = [], []
                                    for idx, row in enumerate(rows):
                                        # event
                                        if idx % 2 == 0:
                                            new_init, new_end = float(row[0]), float(rows[idx + 1][0])
                                            if len(selectedObservations) == 1:
                                                if (new_init < plot_parameters["start time"] and new_end < plot_parameters["start time"]) \
                                                   or \
                                                   (new_init > plot_parameters["end time"] and new_end > plot_parameters["end time"]):
                                                    continue

                                                if new_init < plot_parameters["start time"]:
                                                    new_init = float(plot_parameters["start time"])
                                                if new_end > plot_parameters["end time"]:
                                                    new_end = float(plot_parameters["end time"])

                                            all_event_durations.append( new_end - new_init)

                                        # inter event if same observation
                                        if idx % 2 and idx != len(rows) - 1 and row[1] == rows[idx + 1][1]:
                                            if plot_parameters["start time"] <= row[0] <= plot_parameters["end time"] and plot_parameters["start time"] <= rows[idx + 1][0] <= plot_parameters["end time"]:
                                                all_event_interdurations.append(float(rows[idx + 1][0]) - float(row[0]))

                                            #all_event_interdurations.append(float( rows[idx + 1][0]) - float(row[0]))

                                    out_cat.append({"subject": subject,
                                                "behavior": behavior,
                                                "modifiers": modifier[0],
                                                "duration": round(sum(all_event_durations), 3),
                                                "duration_mean": round(statistics.mean(all_event_durations), 3) if len(all_event_durations) else "NA",
                                                "duration_stdev": round(statistics.stdev(all_event_durations), 3) if len(all_event_durations) > 1 else "NA",
                                                "number": len(all_event_durations),
                                                "inter_duration_mean": round(statistics.mean(all_event_interdurations), 3) if len(all_event_interdurations) else "NA",
                                                "inter_duration_stdev": round(statistics.stdev(all_event_interdurations), 3) if len(all_event_interdurations) > 1 else "NA"
                                                })

                    else:  # no modifiers

                        if POINT in self.eventType(behavior).upper():

                            if len(selectedObservations) > 1:
                                cursor.execute("SELECT occurence,observation FROM events WHERE subject = ? AND code = ? ORDER BY observation, occurence", (subject, behavior))
                            else:
                                cursor.execute("SELECT occurence,observation FROM events WHERE subject = ? AND code = ? AND occurence BETWEEN ? and ? ORDER BY observation, occurence",
                                               (subject, behavior, str(plot_parameters["start time"]), str(plot_parameters["end time"])))

                            rows = list(cursor.fetchall())

                            if len(selectedObservations) == 1:
                                new_rows = []
                                for occurence, observation in rows:
                                    new_occurence = max(float(plot_parameters["start time"]), occurence)
                                    new_occurence = min( new_occurence, float( plot_parameters["end time"]) )
                                    new_rows.append( [new_occurence, observation])
                                rows = list(new_rows)

                            if not len(rows):
                                if not plot_parameters["exclude behaviors"]:
                                    out.append({"subject": subject, "behavior": behavior, "modifiers": "NA",
                                                "duration": "-", "duration_mean": "-", "duration_stdev": "-", "number": 0,
                                                "inter_duration_mean": "-", "inter_duration_stdev": "-"})
                                continue

                            # inter events duration
                            all_event_interdurations = []
                            for idx, row in enumerate(rows):
                                if idx and row[1] == rows[idx - 1][1]:
                                    all_event_interdurations.append(float(row[0]) - float(rows[idx-1][0]))

                            out_cat.append({"subject": subject,
                                        "behavior": behavior,
                                        "modifiers": "NA",
                                        "duration": "-",
                                        "duration_mean": "-",
                                        "duration_stdev": "-",
                                        "number": len(rows),
                                        "inter_duration_mean" : round(statistics.mean(all_event_interdurations), 3) if len(all_event_interdurations) else "NA",
                                        "inter_duration_stdev": round(statistics.stdev(all_event_interdurations), 3) if len(all_event_interdurations) > 1 else "NA"
                                        })


                        if STATE in self.eventType(behavior).upper():
                            cursor.execute( "SELECT occurence, observation FROM events where subject = ? AND code = ? ORDER BY observation, occurence", (subject, behavior))
                            rows = list(cursor.fetchall())

                            if not len(rows):
                                if not plot_parameters["exclude behaviors"]: # include behaviors without events
                                    out.append({"subject": subject , "behavior": behavior,
                                                "modifiers": "NA", "duration": 0, "duration_mean": 0,
                                                "duration_stdev": "NA", "number": 0, "inter_duration_mean": "-",
                                                "inter_duration_stdev": "-"})
                                continue
                            if len(rows) % 2:
                                out.append({"subject": subject, "behavior": behavior, "modifiers": "NA",
                                            "duration": UNPAIRED, "duration_mean": UNPAIRED, "duration_stdev": UNPAIRED,
                                            "number": UNPAIRED, "inter_duration_mean": UNPAIRED,
                                            "inter_duration_stdev": UNPAIRED})
                            else:
                                all_event_durations, all_event_interdurations = [], []
                                for idx, row in enumerate(rows):
                                    # event
                                    if idx % 2 == 0:
                                        new_init, new_end = float(row[0]), float(rows[idx + 1][0])
                                        if len(selectedObservations) == 1:
                                            if ((new_init < plot_parameters["start time"] and new_end < plot_parameters["start time"])
                                               or
                                               (new_init > plot_parameters["end time"] and new_end > plot_parameters["end time"])):
                                                continue

                                            if new_init < plot_parameters["start time"]:
                                                new_init = float(plot_parameters["start time"])
                                            if new_end > plot_parameters["end time"]:
                                                new_end = float(plot_parameters["end time"])

                                        all_event_durations.append( new_end - new_init)

                                    # inter event if same observation
                                    if idx % 2 and idx != len(rows) - 1 and row[1] == rows[idx + 1][1]:
                                        if plot_parameters["start time"] <= row[0] <= plot_parameters["end time"] and plot_parameters["start time"] <= rows[idx + 1][0] <= plot_parameters["end time"]:
                                            all_event_interdurations.append(float(rows[idx + 1][0]) - float(row[0]))

                                out_cat.append({"subject": subject,
                                            "behavior": behavior,
                                            "modifiers": "NA",
                                            "duration": round(sum(all_event_durations), 3),
                                            "duration_mean": round(statistics.mean(all_event_durations), 3) if len(all_event_durations) else "NA",
                                            "duration_stdev": round(statistics.stdev(all_event_durations), 3) if len(all_event_durations) > 1 else "NA",
                                            "number": len(all_event_durations),
                                            "inter_duration_mean": round(statistics.mean(all_event_interdurations), 3) if len(all_event_interdurations) else "NA",
                                            "inter_duration_stdev": round(statistics.stdev(all_event_interdurations), 3) if len(all_event_interdurations) > 1 else "NA"
                                            })

                out += out_cat

                if by_category: # and flagCategories:

                    for behav in out_cat:

                        try:
                            category = [self.pj[ETHOGRAM][x]["category"] for x in self.pj[ETHOGRAM] if "category" in self.pj[ETHOGRAM][x] and self.pj[ETHOGRAM][x]["code"] == behav['behavior']][0]
                        except:
                            category = ""

                        if category in categories[subject]:
                            if behav["duration"] != "-" and categories[subject][category]["duration"] != "-":
                                categories[subject][category]["duration"] += behav["duration"]
                            else:
                                categories[subject][category]["duration"] = "-"
                            categories[subject][category]["number"] += behav["number"]
                        else:
                            categories[subject][category] = {"duration": behav["duration"], "number": behav["number"]}

            out_sorted = []
            for subject in plot_parameters["selected subjects"]:
                for behavior in plot_parameters["selected behaviors"]:
                    for row in out:
                        if row['subject'] == subject and row['behavior'] == behavior:
                            out_sorted.append(row)


            ### http://stackoverflow.com/questions/673867/python-arbitrary-order-by
            return out_sorted, categories


        result, selectedObservations = self.selectObservations(MULTIPLE)

        logging.debug("Selected observations: {0}".format(selectedObservations))

        if not selectedObservations:
            return

        selectedObsTotalMediaLength = Decimal("0.0")

        for obsId in selectedObservations:
            if self.pj[OBSERVATIONS][ obsId ][TYPE] == MEDIA:
                totalMediaLength = self.observationTotalMediaLength(obsId)
                logging.debug("media length for {0} : {1}".format(obsId,totalMediaLength ))
            else: # LIVE
                if self.pj[OBSERVATIONS][obsId][EVENTS]:
                    totalMediaLength = max(self.pj[OBSERVATIONS][obsId][EVENTS])[0]
                else:
                    totalMediaLength = Decimal("0.0")
            if totalMediaLength in [0, -1]:
                selectedObsTotalMediaLength = -1
                break
            selectedObsTotalMediaLength += totalMediaLength

        if selectedObsTotalMediaLength == -1: # an observation media length is not available
            # propose to user to use max event time
            if dialog.MessageDialog(programName, "A media length is not available.<br>Use last event time as media length?", [YES, NO]) == YES:
                maxTime = 0 # max length for all events all subjects
                for obsId in selectedObservations:
                    if self.pj[OBSERVATIONS][obsId][EVENTS]:
                        maxTime += max(self.pj[OBSERVATIONS][obsId][EVENTS])[0]
                logging.debug("max time all events all subjects: {0}".format(maxTime))
                selectedObsTotalMediaLength = maxTime
            else:
                selectedObsTotalMediaLength = 0

        logging.debug("selectedObsTotalMediaLength: {}".format(selectedObsTotalMediaLength))

        if len(selectedObservations) > 1:
            plot_parameters = self.choose_obs_subj_behav_category(selectedObservations, maxTime=0, by_category=(mode == "by_category"))
            flagGroup = dialog.MessageDialog(programName, "Group observations?", [YES, NO]) == YES
        else:
            plot_parameters = self.choose_obs_subj_behav_category(selectedObservations, maxTime=selectedObsTotalMediaLength, by_category=(mode == "by_category"))

        if not plot_parameters["selected subjects"] or not plot_parameters["selected behaviors"]:
            return

        # check if time_budget window must be used
        if (len(selectedObservations) > 1 and flagGroup) or (len(selectedObservations) == 1):
            cursor = self.loadEventsInDB(plot_parameters["selected subjects"], selectedObservations, plot_parameters["selected behaviors"])
            out, categories = time_budget_analysis_by_category(cursor, plot_parameters, by_category=(mode == "by_category"))

        else:

            items = ("Tab Separated Values (*.tsv)", "Comma separated values (*.csv)", "Open Document Spreadsheet (*.ods)", "Microsoft Excel (*.xls)", "HTML (*.html)")
            item, ok = QInputDialog.getItem(self, "Time budget analysis format", "Available formats", items, 0, False)
            if not ok:
                return
            outputFormat = re.sub(".* \(\*\.", "", item)[:-1]

            flagWorkBook = False
            if outputFormat in ["xls", "ods"]:
                flagWorkBook = dialog.MessageDialog(programName, "Choose the type of file", ["Single sheets", "Workbook"]) == "Workbook"
                if flagWorkBook:
                    workbook = tablib.Databook()
                    if outputFormat == "xls":
                        filters = "Microsoft Excel XLS (*.xls);;All files (*)"
                    if outputFormat == "ods":
                        filters = "Open Document Spreadsheet ODS (*.ods);;All files (*)"

                    if QT_VERSION_STR[0] == "4":
                        WBfileName, filter_ = QFileDialog(self).getSaveFileNameAndFilter(self, "Save Time budget analysis", "", filters)
                    else:
                        WBfileName, filter_ = QFileDialog(self).getSaveFileName(self, "Save Time budget analysis", "", filters)

                    if not WBfileName:
                        return


            if not flagWorkBook:
                exportDir = QFileDialog(self).getExistingDirectory(self, "Choose a directory to save the time budget analysis", os.path.expanduser("~"), options=QFileDialog.ShowDirsOnly)
                if not exportDir:
                    return


            if mode == "by_behavior":
                    fields = ["subject", "behavior",  "modifiers", "number", "duration", "duration_mean", "duration_stdev", "inter_duration_mean", "inter_duration_stdev"]
            if mode == "by_category":
                    fields = ["subject", "category",  "number", "duration"]

            for obsId in selectedObservations:

                cursor = self.loadEventsInDB(plot_parameters["selected subjects"], [obsId], plot_parameters["selected behaviors"])
                out, categories = time_budget_analysis_by_category(cursor, plot_parameters, by_category=(mode == "by_category"))

                rows = []

                # observation id
                rows.append(["Observations:"])
                rows.append([obsId])
                rows.append([""])

                #indep variables
                if INDEPENDENT_VARIABLES in self.pj[OBSERVATIONS][obsId]:
                    rows.append(["Independent variables:"])
                    for var in self.pj[OBSERVATIONS][obsId][INDEPENDENT_VARIABLES]:
                        rows.append([var, self.pj[OBSERVATIONS][obsId][INDEPENDENT_VARIABLES][var]])
                rows.append([""])
                rows.append([""])
                rows.append(["Time budget:"])

                if mode == "by_behavior":

                    rows.append(fields + ["% of total media length"])
                    #data.headers = fields + ["% of total media length"]

                    for row in out:
                        values = []
                        for field in fields:
                            values.append(str(row[field]).replace(" ()", ""))

                        # % of total time
                        if row["duration"] != "-" and row["duration"] != 0 and row["duration"] != UNPAIRED and selectedObsTotalMediaLength:
                            if len(selectedObservations) > 1:
                                values.append(round(row["duration"] / float(selectedObsTotalMediaLength) * 100, 1))
                            else:
                                values.append(round(row["duration"] / float(plot_parameters["end time"] - plot_parameters["start time"]) * 100, 1))
                        else:
                            values.append("-")
                        rows.append(values)

                if mode == "by_category":
                    rows.append = fields
                    #data.headers = fields # + ["% of total media length"]
                    for subject in categories:

                        for category in categories[subject]:
                            values = []
                            values.append(subject)
                            if category == "":
                                values.append("No category")
                            else:
                                values.append(category)

                            values.append(categories[subject][category]["number"])
                            values.append(categories[subject][category]["duration"])

                            rows.append(values)

                data = tablib.Dataset()
                data.title = obsId
                for row in rows:
                    data.append(complete(row, max([len(r) for r in rows])))

                if flagWorkBook:
                    # check data title for worksheet name
                    if len(data.title) > 31:
                        data.title = data.title[:31]
                    for forbidden_char in r"\/*[]:?":
                        data.title = data.title.replace(forbidden_char, " ")

                    workbook.add_sheet(data)
                else:

                    fileName = exportDir + os.sep + safeFileName(obsId) + "." + outputFormat

                    if outputFormat == "tsv":
                        with open(fileName, "wb") as f:
                            f.write(str.encode(data.tsv))

                    if outputFormat == "csv":
                        with open(fileName, "wb") as f:
                            f.write(str.encode(data.csv))

                    if outputFormat == "ods":
                        with open(fileName, "wb") as f:
                            f.write(data.ods)

                    if outputFormat == "html":
                        with open(fileName, "wb") as f:
                            f.write(str.encode(data.html))

                    if outputFormat == "xls":

                        if len(data.title) > 31:
                            data.title = data.title[:31]
                            QMessageBox.warning(None, programName, ("The worksheet name <b>{0}</b> was shortened to <b>{1}</b> due to XLS format limitations.\n"
                                                                    "The limit on worksheet name length is 31 characters").format(obsId, data.title),
                                                 QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)

                        for forbidden_char in r"\/*[]:?":
                            data.title = data.title.replace(forbidden_char, " ")
                        with open(fileName, "wb") as f:
                            f.write(data.xls)

            if flagWorkBook:
                if outputFormat == "xls":
                    with open(WBfileName, "wb") as f:
                        f.write(workbook.xls)
                if outputFormat == "ods":
                    with open(WBfileName, "wb") as f:
                        f.write(workbook.ods)
            return


        # widget for results visualization
        self.tb = timeBudgetResults(logging.getLogger().getEffectiveLevel(), self.pj)

        # observations list
        self.tb.label.setText("Selected observations")
        for obs in selectedObservations:
            self.tb.lw.addItem(obs)

        # media length
        if len(selectedObservations) > 1:
            if selectedObsTotalMediaLength:
                if self.timeFormat == HHMMSS:
                    self.tb.lbTotalObservedTime.setText("Total media length: {}".format(seconds2time(selectedObsTotalMediaLength)))
                if self.timeFormat == S:
                    self.tb.lbTotalObservedTime.setText("Total media length: {:0.3f}".format(float(selectedObsTotalMediaLength)))
            else:
                self.tb.lbTotalObservedTime.setText("Total media length: not available")
        else:

            if self.timeFormat == HHMMSS:
                self.tb.lbTotalObservedTime.setText("Analysis from {} to {}".format(seconds2time(plot_parameters["start time"]), seconds2time(plot_parameters["end time"])))
            if self.timeFormat == S:
                self.tb.lbTotalObservedTime.setText("Analysis from {:0.3f} to {:0.3f} s".format(float(plot_parameters["start time"]), float(plot_parameters["end time"])))



        if mode == "by_behavior":
            tb_fields = ["Subject", "Behavior", "Modifiers", "Total number", "Total duration (s)",
                         "Duration mean (s)", "Duration std dev", "inter-event intervals mean (s)",
                         "inter-event intervals std dev", "% of total media length"]

            fields = ["subject", "behavior",  "modifiers", "number", "duration", "duration_mean", "duration_stdev", "inter_duration_mean", "inter_duration_stdev"]
            self.tb.twTB.setColumnCount(len(tb_fields))
            self.tb.twTB.setHorizontalHeaderLabels(tb_fields)

            for row in out:
                self.tb.twTB.setRowCount(self.tb.twTB.rowCount() + 1)
                column = 0
                for field in fields:
                    item = QTableWidgetItem(str(row[field]).replace(" ()", ""))
                    # no modif allowed
                    item.setFlags(Qt.ItemIsEnabled)
                    self.tb.twTB.setItem(self.tb.twTB.rowCount() - 1, column , item)
                    column += 1

                # % of total time
                if row["duration"] != "-" and row["duration"] != 0 and row["duration"] != UNPAIRED and selectedObsTotalMediaLength:
                    if len(selectedObservations) > 1:
                        item = QTableWidgetItem(str(round(row["duration"] / float(selectedObsTotalMediaLength) * 100, 1)))
                    else:
                        item = QTableWidgetItem(str(round(row["duration"] / float(plot_parameters["end time"] - plot_parameters["start time"]) * 100, 1)))
                else:
                    item = QTableWidgetItem("-")

                item.setFlags(Qt.ItemIsEnabled)
                self.tb.twTB.setItem(self.tb.twTB.rowCount() - 1, column, item)

        if mode == "by_category":
            tb_fields = ["Subject", "Category", "Total number", "Total duration (s)"]
            fields = ["number", "duration"]
            self.tb.twTB.setColumnCount(len(tb_fields))
            self.tb.twTB.setHorizontalHeaderLabels(tb_fields)

            for subject in categories:

                for category in categories[subject]:

                    self.tb.twTB.setRowCount(self.tb.twTB.rowCount() + 1)

                    column = 0
                    item = QTableWidgetItem(subject)
                    item.setFlags(Qt.ItemIsEnabled)
                    self.tb.twTB.setItem(self.tb.twTB.rowCount() - 1, column , item)

                    column = 1
                    if category == "":
                        item = QTableWidgetItem("No category")
                    else:
                        item = QTableWidgetItem(category)
                    item.setFlags(Qt.ItemIsEnabled)
                    self.tb.twTB.setItem(self.tb.twTB.rowCount() - 1, column , item)

                    for field in fields:
                        column += 1
                        item = QTableWidgetItem(str(categories[subject][category][field]))
                        item.setFlags(Qt.ItemIsEnabled)
                        item.setTextAlignment(Qt.AlignRight|Qt.AlignVCenter)
                        self.tb.twTB.setItem(self.tb.twTB.rowCount() - 1, column , item)



        self.tb.twTB.resizeColumnsToContents()

        self.tb.show()








    def observationTotalMediaLength(self, obsId):
        '''
        total media length for observation
        if media length not available return 0

        return total media length in s
        '''

        totalMediaLength, totalMediaLength1, totalMediaLength2 = Decimal("0.0"), Decimal("0.0"), Decimal("0.0")

        for mediaFile in self.pj[OBSERVATIONS][obsId][FILE][PLAYER1]:
            mediaLength = 0
            try:
                mediaLength = self.pj[OBSERVATIONS][obsId]["media_info"]["length"][mediaFile]
            except:
                nframe, videoTime, videoDuration, fps, hasVideo, hasAudio = accurate_media_analysis( self.ffmpeg_bin, mediaFile)
                if "media_info" not in self.pj[OBSERVATIONS][obsId]:
                    self.pj[OBSERVATIONS][obsId]["media_info"] = {"length": {}, "fps": {}}
                    if "length" not in self.pj[OBSERVATIONS][obsId]["media_info"]:
                        self.pj[OBSERVATIONS][obsId]["media_info"]["length"] = {}
                    if "fps" not in self.pj[OBSERVATIONS][obsId]["media_info"]:
                        self.pj[OBSERVATIONS][obsId]["media_info"]["fps"] = {}

                self.pj[OBSERVATIONS][obsId]["media_info"]["length"][mediaFile] = videoDuration
                self.pj[OBSERVATIONS][obsId]["media_info"]["fps"][mediaFile] = fps

                mediaLength = videoDuration

                '''
                try:
                    fileContentMD5 = hashfile( mediaFile , hashlib.md5())                  # md5 sum of file content
                    mediaLength = self.pj["project_media_file_info"][fileContentMD5]["video_length"] / 1000
                except:
                    if os.path.isfile(mediaFile):
                        try:
                            instance = vlc.Instance()
                            media = instance.media_new(mediaFile)
                            media.parse()
                            mediaLength = media.get_duration()/1000
                        except:
                            totalMediaLength1 = -1
                            break
                    else:
                        totalMediaLength1 = -1
                        break
                '''

            totalMediaLength1 += Decimal(mediaLength)

        for mediaFile in self.pj[OBSERVATIONS][obsId][FILE][PLAYER2]:
            mediaLength = 0
            try:
                mediaLength = self.pj[OBSERVATIONS][obsId]["media_info"]["length"][mediaFile]
            except:
                nframe, videoTime, videoDuration, fps, hasVideo, hasAudio = accurate_media_analysis( self.ffmpeg_bin, mediaFile)
                if "media_info" not in self.pj[OBSERVATIONS][obsId]:
                    self.pj[OBSERVATIONS][obsId]["media_info"] = {"length": {}, "fps": {}}
                    if "length" not in self.pj[OBSERVATIONS][obsId]["media_info"]:
                        self.pj[OBSERVATIONS][obsId]["media_info"]["length"] = {}
                    if "fps" not in self.pj[OBSERVATIONS][obsId]["media_info"]:
                        self.pj[OBSERVATIONS][obsId]["media_info"]["fps"] = {}

                self.pj[OBSERVATIONS][obsId]["media_info"]["length"][mediaFile] = videoDuration
                self.pj[OBSERVATIONS][obsId]["media_info"]["fps"][mediaFile] = fps

                mediaLength = videoDuration



                '''
                try:
                    fileContentMD5 = hashfile( mediaFile , hashlib.md5())                 # md5 sum of file content
                    mediaLength = self.pj['project_media_file_info'][fileContentMD5]['video_length']/1000
                except:
                    if os.path.isfile(mediaFile):
                        try:
                            instance = vlc.Instance()
                            media = instance.media_new(mediaFile)
                            media.parse()
                            mediaLength = media.get_duration()/1000
                        except:
                            totalMediaLength2 = -1
                            break
                    else:
                        totalMediaLength2 = -1
                        break
                '''

            totalMediaLength2 += Decimal(mediaLength)

        if  totalMediaLength1  == -1 or totalMediaLength2 == -1:
            totalMediaLength = -1
        else:
            totalMediaLength = max( totalMediaLength1, totalMediaLength2 )

        return totalMediaLength

    def plot_events(self):
        """
        plot events with matplotlib
        """

        def plot_time_ranges(obs, obsId, minTime, videoLength, excludeBehaviorsWithoutEvents, line_width):
            """
            create "hlines" matplotlib plot
            """

            import matplotlib.pyplot as plt
            import matplotlib.transforms as mtransforms
            from matplotlib import dates
            import numpy as np

            LINE_WIDTH = line_width
            all_behaviors, observedBehaviors = [], []
            maxTime = 0  # max time in all events of all subjects

            # all behaviors defined in project without modifiers
            all_project_behaviors = [self.pj[ETHOGRAM][idx]["code"] for idx in sorted_keys(self.pj[ETHOGRAM])]
            all_project_subjects = [NO_FOCAL_SUBJECT] + [self.pj[SUBJECTS][idx]["name"] for idx in sorted_keys(self.pj[SUBJECTS])]

            for subject in obs.keys():

                for behavior_modifiers_json in obs[subject].keys():

                    behavior_modifiers = json.loads(behavior_modifiers_json)

                    if not excludeBehaviorsWithoutEvents:
                        observedBehaviors.append(behavior_modifiers_json)
                    else:
                        if obs[subject][behavior_modifiers_json]:
                            observedBehaviors.append(behavior_modifiers_json)

                    if not behavior_modifiers_json in all_behaviors:
                        all_behaviors.append(behavior_modifiers_json)

                    for t1, t2 in obs[subject][behavior_modifiers_json]:
                        maxTime = max(maxTime, t1, t2)

                observedBehaviors.append("")

            all_behaviors2 = [json.loads(x)[0] for x in all_behaviors]
            all_behaviors = ['["{}"]'.format(x) for x in all_project_behaviors if x in all_behaviors2]

            lbl = []
            if excludeBehaviorsWithoutEvents:
                for behav_modif_json in observedBehaviors:
                    behav_modif = json.loads(behav_modif_json)
                    if len(behav_modif) == 2:
                        lbl.append("{0} ({1})".format(behav_modif[0], behav_modif[1]))
                    else:
                        lbl.append(behav_modif[0])

            else:
                all_behaviors.append('[""]') # empty json list element
                for behav_modif_json in all_behaviors:
                    behav_modif = json.loads(behav_modif_json)
                    if len(behav_modif) == 2:
                        lbl.append("{0} ({1})".format(behav_modif[0], behav_modif[1]))
                    else:
                        lbl.append(behav_modif[0])
                lbl = lbl[:] * len(obs)


            lbl = lbl[:-1]  # remove last empty line

            fig = plt.figure(figsize=(20, 10))
            fig.suptitle("Time diagram of observation {}".format(obsId), fontsize=14)
            ax = fig.add_subplot(111)
            labels = ax.set_yticklabels(lbl)

            ax.set_ylabel("Behaviors")

            if self.timeFormat == HHMMSS:
                fmtr = dates.DateFormatter("%H:%M:%S") # %H:%M:%S:%f
                ax.xaxis.set_major_formatter(fmtr)
                ax.set_xlabel("Time (hh:mm:ss)")
            else:
                ax.set_xlabel("Time (s)")

            plt.ylim(len(lbl), -0.5)

            if not videoLength:
                videoLength = maxTime

            if self.pj[OBSERVATIONS][obsId]["time offset"]:
                t0 = round(self.pj[OBSERVATIONS][obsId]["time offset"] + minTime)
                t1 = round(self.pj[OBSERVATIONS][obsId]["time offset"] + videoLength + 2)
            else:
                t0 = round(minTime)
                t1 = round(videoLength)
            subjectPosition = t0 + (t1 - t0) * 0.05

            if self.timeFormat == HHMMSS:
                t0d = datetime.datetime(1970, 1, 1, int(t0 / 3600), int((t0 - int(t0 / 3600) * 3600)/60), int(t0 % 60), round(round(t0 % 1,3)*1000000))
                t1d = datetime.datetime(1970, 1, 1, int(t1 / 3600), int((t1 - int(t1 / 3600) * 3600)/60), int(t1 % 60), round(round(t1 % 1,3)*1000000))
                subjectPositiond = datetime.datetime(1970, 1, 1, int(subjectPosition / 3600), int((subjectPosition - int(subjectPosition / 3600) * 3600)/60), int(subjectPosition % 60), round(round(subjectPosition % 1,3)*1000000))

            if self.timeFormat == S:
                t0d, t1d = t0, t1
                subjectPositiond = subjectPosition

            plt.xlim(t0d, t1d)
            plt.yticks(range(len(lbl) + 1), np.array(lbl))

            count = 0
            flagFirstSubject = True

            for subject in all_project_subjects:
                if subject not in obs.keys():
                    continue

                if not flagFirstSubject:
                    if excludeBehaviorsWithoutEvents:
                        count += 1
                    ax.axhline(y=(count-1), linewidth=1, color="black")
                    ax.hlines(np.array([count]), np.array([0]), np.array([0]), lw=LINE_WIDTH, color=col)
                else:
                    flagFirstSubject = False

                ax.text(subjectPositiond, count - 0.5, subject)

                behaviors = obs[subject]

                x1, x2, y, col, pointsx, pointsy, guide = [], [], [], [], [], [], []
                col_count = 0

                for bm_json in all_behaviors:
                    if bm_json in obs[subject]:
                        if obs[subject][bm_json]:
                            for t1, t2 in obs[subject][bm_json]:
                                if t1 == t2:
                                    pointsx.append(t1)
                                    pointsy.append(count)
                                    ax.axhline(y=count, linewidth=1, color="lightgray", zorder=-1)
                                else:
                                    x1.append(t1)
                                    x2.append(t2)
                                    y.append(count)

                                    col.append(BEHAVIORS_PLOT_COLORS[all_project_behaviors.index(json.loads(bm_json)[0]) % len(BEHAVIORS_PLOT_COLORS)])

                                    ax.axhline(y=count, linewidth=1, color="lightgray", zorder=-1)
                            count += 1
                        else:
                            x1.append(0)
                            x2.append(0)
                            y.append(count)
                            col.append("white")
                            ax.axhline(y=count ,linewidth=1, color="lightgray", zorder=-1)
                            count += 1

                    else:
                        if not excludeBehaviorsWithoutEvents:
                            x1.append(0)
                            x2.append(0)
                            y.append(count)
                            col.append("white")
                            ax.axhline(y=count ,linewidth=1, color="lightgray", zorder=-1)
                            count += 1

                    col_count += 1

                if self.timeFormat == HHMMSS:
                    ax.hlines(np.array(y), np.array([datetime.datetime(1970, 1, 1, int(p/3600), int((p-int(p/3600)*3600)/60), int(p%60), round(round(p%1,3)*1e6)) for p in x1]),
                    np.array([datetime.datetime(1970, 1, 1, int(p/3600), int((p-int(p/3600)*3600)/60), int(p%60), round(round(p%1,3)*1e6)) for p in x2]),
                    lw=LINE_WIDTH, color=col)

                if self.timeFormat == S:
                    ax.hlines(np.array(y), np.array(x1), np.array(x2), lw=LINE_WIDTH, color=col)

                if self.timeFormat == HHMMSS:
                    ax.plot(np.array([datetime.datetime(1970, 1, 1, int(p/3600), int((p-int(p/3600)*3600)/60), int(p%60), round(round(p%1,3)*1e6)) for p in pointsx]), pointsy, "r^")

                if self.timeFormat == S:
                    ax.plot(pointsx, pointsy, "r^")

                #ax.axhline(y=y[-1] + 0.5,linewidth=1, color='black')

            def on_draw(event):

                # http://matplotlib.org/faq/howto_faq.html#move-the-edge-of-an-axes-to-make-room-for-tick-labels
                bboxes = []
                for label in labels:
                    bbox = label.get_window_extent()
                    bboxi = bbox.inverse_transformed(fig.transFigure)
                    bboxes.append(bboxi)

                bbox = mtransforms.Bbox.union(bboxes)
                if fig.subplotpars.left < bbox.width:
                    fig.subplots_adjust(left=1.1*bbox.width)
                    fig.canvas.draw()
                return False

            fig.canvas.mpl_connect("draw_event", on_draw)
            plt.show()

            return True

        result, selectedObservations = self.selectObservations(SELECT1)

        logging.debug("Selected observations: {0}".format(selectedObservations))

        if not selectedObservations:
            return

        if not self.pj[OBSERVATIONS][ selectedObservations[0] ][EVENTS]:
            QMessageBox.warning(self, programName, "There are no events in the selected observation")
            return

        for obsId in selectedObservations:
            if self.pj[OBSERVATIONS][ obsId ][TYPE] == MEDIA:
                totalMediaLength = self.observationTotalMediaLength(obsId)
            else: # LIVE
                if self.pj[OBSERVATIONS][ obsId ][EVENTS]:
                    totalMediaLength = max(self.pj[OBSERVATIONS][ obsId ][EVENTS])[0]
                else:
                    totalMediaLength = Decimal("0.0")

        if totalMediaLength == -1 :
            totalMediaLength = 0

        logging.debug("totalMediaLength: {0}".format(totalMediaLength))

        plot_parameters = self.choose_obs_subj_behav_category(selectedObservations, totalMediaLength)

        logging.debug("totalMediaLength: {0} s".format(totalMediaLength))

        totalMediaLength = int(totalMediaLength)

        if not plot_parameters["selected subjects"] or not plot_parameters["selected behaviors"]:
            return

        cursor = self.loadEventsInDB(plot_parameters["selected subjects"], selectedObservations, plot_parameters["selected behaviors"])

        o = {}

        for subject in plot_parameters["selected subjects"]:

            o[subject] = {}

            for behavior in plot_parameters["selected behaviors"]:

                if plot_parameters["include modifiers"]:

                    cursor.execute( "SELECT distinct modifiers FROM events WHERE subject = ? AND code = ?", (subject, behavior) )
                    distinct_modifiers = list(cursor.fetchall())

                    for modifier in distinct_modifiers:
                        cursor.execute("SELECT occurence FROM events WHERE subject = ? AND code = ? AND modifiers = ? ORDER BY observation, occurence",
                                      (subject, behavior, modifier[0]))

                        rows = cursor.fetchall()

                        if modifier[0]:
                            behaviorOut = [behavior, modifier[0].replace("|", ",")]

                        else:
                            behaviorOut = [behavior]

                        behaviorOut_json = json.dumps(behaviorOut)

                        if not behaviorOut_json in o[subject]:
                            o[subject][behaviorOut_json] = []

                        for idx, row in enumerate(rows):
                            if POINT in self.eventType(behavior).upper():
                                o[subject][behaviorOut_json].append([row[0], row[0]])  # for point event start = end

                            if STATE in self.eventType(behavior).upper():
                                if idx % 2 == 0:
                                    try:
                                        o[subject][behaviorOut_json].append([row[0], rows[idx + 1][0]])
                                    except:
                                        if NO_FOCAL_SUBJECT in subject:
                                            sbj = ""
                                        else:
                                            sbj = "for subject <b>{0}</b>".format(subject)
                                        QMessageBox.critical(self, programName,
                                            "The STATE behavior <b>{0}</b> is not paired {1}".format(behaviorOut, sbj))
                else:
                    cursor.execute("SELECT occurence FROM events WHERE subject = ? AND code = ?  ORDER BY observation, occurence",
                                  (subject, behavior))
                    rows = list(cursor.fetchall())

                    if not len(rows) and plot_parameters["exclude behaviors"]:
                        continue

                    if STATE in self.eventType(behavior).upper() and len(rows) % 2:
                        continue

                    behaviorOut = [behavior]
                    behaviorOut_json = json.dumps(behaviorOut)

                    if not behaviorOut_json in o[subject]:
                        o[subject][behaviorOut_json] = []

                    for idx, row in enumerate(rows):
                        if POINT in self.eventType(behavior).upper():
                            o[subject][behaviorOut_json].append([row[0], row[0]])   # for point event start = end
                        if STATE in self.eventType(behavior).upper():
                            if idx % 2 == 0:
                                o[subject][behaviorOut_json].append([row[0], rows[idx + 1][0]])

        logging.debug("intervals: {}".format(o))
        logging.debug("totalMediaLength: {}".format(plot_parameters["end time"]))
        logging.debug("excludeBehaviorsWithoutEvents: {}".format(plot_parameters["exclude behaviors"]))

        if not plot_time_ranges(o,
                                selectedObservations[0],
                                plot_parameters["start time"],
                                plot_parameters["end time"],
                                plot_parameters["exclude behaviors"],
                                line_width=10):
            QMessageBox.warning(self, programName, "Check events")


    def convert_time_to_decimal(self, pj):
        """
        convert time from float to decimal
        """

        for obsId in pj[OBSERVATIONS]:
            if "time offset" in pj[OBSERVATIONS][obsId]:
                pj[OBSERVATIONS][obsId]["time offset"] = Decimal(str(pj[OBSERVATIONS][obsId]["time offset"]) )

            for idx, event in enumerate(pj[OBSERVATIONS][obsId][EVENTS]):
                pj[OBSERVATIONS][obsId][EVENTS][idx][pj_obs_fields["time"]] = Decimal(str(pj[OBSERVATIONS][obsId][EVENTS][idx][pj_obs_fields["time"]]))

        return pj


    def open_project_json(self, projectFileName):
        """
        open project json
        """
        logging.info("open project: {0}".format(projectFileName))

        if not os.path.isfile(projectFileName):
            QMessageBox.warning(self, programName, "File not found")
            return

        s = open(projectFileName, "r").read()

        try:
            self.pj = json.loads(s)
        except:
            QMessageBox.critical(self, programName, "This project file seems corrupted")
            return

        self.projectChanged = False

        # transform time to decimal
        self.pj = self.convert_time_to_decimal(self.pj)

        ''' 2016-10-13 moved in function convert_time_to_decimal
        for obs in self.pj[OBSERVATIONS]:
            self.pj[OBSERVATIONS][obs]["time offset"] = Decimal(str(self.pj[OBSERVATIONS][obs]["time offset"]) )

            for idx,event in enumerate(self.pj[OBSERVATIONS][obs][EVENTS]):
                self.pj[OBSERVATIONS][obs][EVENTS][idx][pj_obs_fields["time"]] = Decimal(str(self.pj[OBSERVATIONS][obs][EVENTS][idx][pj_obs_fields["time"]]))
        '''

        # add coding_map key to old project files
        if not "coding_map" in self.pj:
            self.pj["coding_map"] = {}
            self.projectChanged = True

        # add subject description
        if 'project_format_version' in self.pj:
            for idx in [x for x in self.pj[SUBJECTS]]:
                if not 'description' in self.pj[SUBJECTS][ idx ] :
                    self.pj[SUBJECTS][ idx ]['description'] = ""
                    self.projectChanged = True

        # check if project file version is newer than current BORIS project file version
        if 'project_format_version' in self.pj and Decimal(self.pj['project_format_version']) > Decimal(project_format_version):
            QMessageBox.critical(self, programName, "This project file was created with a more recent version of BORIS.\nUpdate your version of BORIS to load it")
            self.pj = {"time_format": "hh:mm:ss",
            "project_date": "",
            "project_name": "",
            "project_description": "",
            "subjects_conf" : {},
            "behaviors_conf": {},
            "observations": {}}
            return


        # check if old version  v. 0 *.obs
        if "project_format_version" not in self.pj:

            # convert VIDEO, AUDIO -> MEDIA
            self.pj['project_format_version'] = project_format_version
            self.projectChanged = True

            for obs in [x for x in self.pj[OBSERVATIONS]]:

                # remove 'replace audio' key
                if "replace audio" in self.pj[OBSERVATIONS][obs]:
                    del self.pj[OBSERVATIONS][obs]['replace audio']

                if self.pj[OBSERVATIONS][obs][TYPE] in ['VIDEO','AUDIO']:
                    self.pj[OBSERVATIONS][obs][TYPE] = MEDIA

                # convert old media list in new one
                if len( self.pj[OBSERVATIONS][obs][FILE] ):
                    d1 = { PLAYER1:  [self.pj[OBSERVATIONS][obs][FILE][0]] }

                if len( self.pj[OBSERVATIONS][obs][FILE] ) == 2:
                    d1[PLAYER2] =  [self.pj[OBSERVATIONS][obs][FILE][1]]

                self.pj[OBSERVATIONS][obs][FILE] = d1

            # convert VIDEO, AUDIO -> MEDIA
            for idx in [x for x in self.pj[SUBJECTS]]:
                key, name = self.pj[SUBJECTS][idx]
                self.pj[SUBJECTS][idx] = {"key": key, "name": name, "description": ""}
            QMessageBox.information(self, programName, ("The project file was converted to the new format (v. {}) in use with your version of BORIS.<br>"
                                                        "Choose a new file name for saving it.").format(project_format_version))
            projectFileName = ''

        '''
        if not 'project_media_file_info' in self.pj:
            self.pj['project_media_file_info'] = {}
            self.projectChanged = True

        if not 'project_media_file_info' in self.pj:
            for obs in self.pj[OBSERVATIONS]:
                if 'media_file_info' in self.pj[OBSERVATIONS][obs]:
                    for h in self.pj[OBSERVATIONS][obs]['media_file_info']:
                        self.pj['project_media_file_info'][h] = self.pj[OBSERVATIONS][obs]['media_file_info'][h]
                        self.projectChanged = True
        '''

        for obs in self.pj[OBSERVATIONS]:
            if not "time offset second player" in self.pj[OBSERVATIONS][obs]:
                self.pj[OBSERVATIONS][obs]["time offset second player"] = Decimal("0.0")
                self.projectChanged = True


        # if one file is present in player #1 -> set "media_info" key with value of media_file_info

        project_updated = False


        for obs in self.pj[OBSERVATIONS]:
            if self.pj[OBSERVATIONS][obs][TYPE] in [MEDIA] and "media_info" not in self.pj[OBSERVATIONS][obs]:
                self.pj[OBSERVATIONS][obs]['media_info'] = {"length": {}, "fps": {}, "hasVideo": {}, "hasAudio": {}}
                for player in [PLAYER1, PLAYER2]:
                    for media_file_path in self.pj[OBSERVATIONS][obs]["file"][player]:
                        nframe, videoTime, videoDuration, fps, hasVideo, hasAudio = accurate_media_analysis(self.ffmpeg_bin, media_file_path)
                        print(media_file_path, nframe, videoTime, videoDuration, fps, hasVideo, hasAudio)
                        if videoDuration:
                            self.pj[OBSERVATIONS][obs]['media_info']["length"][media_file_path] = videoDuration
                            self.pj[OBSERVATIONS][obs]['media_info']["fps"][media_file_path] = fps
                            self.pj[OBSERVATIONS][obs]['media_info']["hasVideo"][media_file_path] = hasVideo
                            self.pj[OBSERVATIONS][obs]['media_info']["hasAudio"][media_file_path] = hasAudio
                            project_updated, self.projectChanged = True, True
                        else:  # file path not found
                            if (len(self.pj[OBSERVATIONS][obs]["media_file_info"]) == 1
                                and len(self.pj[OBSERVATIONS][obs]["file"][PLAYER1]) == 1
                                and len(self.pj[OBSERVATIONS][obs]["file"][PLAYER2]) == 0):
                                    media_md5_key = list(self.pj[OBSERVATIONS][obs]['media_file_info'].keys())[0]
                                    # duration
                                    self.pj[OBSERVATIONS][obs]['media_info'] = {"length": {media_file_path:
                                             self.pj[OBSERVATIONS][obs]['media_file_info'][media_md5_key]['video_length']/1000}}
                                    project_updated, self.projectChanged = True, True

                                    # FPS
                                    if "nframe" in self.pj[OBSERVATIONS][obs]["media_file_info"][media_md5_key]:
                                        self.pj[OBSERVATIONS][obs]['media_info']['fps'] = {media_file_path:
                                             self.pj[OBSERVATIONS][obs]['media_file_info'][media_md5_key]['nframe']
                                             / (self.pj[OBSERVATIONS][obs]['media_file_info'][media_md5_key]['video_length']/1000)}
                                    else:
                                        self.pj[OBSERVATIONS][obs]['media_info']['fps'] = {media_file_path: 0}


        '''
            try:
                if (not "media_info" in self.pj[OBSERVATIONS][obs]
                    and len(self.pj[OBSERVATIONS][obs]["media_file_info"]) == 1
                    and len(self.pj[OBSERVATIONS][obs]["file"][PLAYER1]) == 1
                    and len(self.pj[OBSERVATIONS][obs]["file"][PLAYER2]) == 0):
                        self.pj[OBSERVATIONS][obs]['media_info'] = {"length": {self.pj[OBSERVATIONS][obs]['file'][PLAYER1][0]:
                               self.pj[OBSERVATIONS][obs]['media_file_info'][list(self.pj[OBSERVATIONS][obs]['media_file_info'].keys())[0]]['video_length']/1000}}
                        # FPS
                        if "nframe" in self.pj[OBSERVATIONS][obs]["media_file_info"][list(self.pj[OBSERVATIONS][obs]["media_file_info"].keys())[0]]:
                            self.pj[OBSERVATIONS][obs]['media_info']['fps'] = {self.pj[OBSERVATIONS][obs]['file'][PLAYER1][0]:
                                self.pj[OBSERVATIONS][obs]['media_file_info'][list(self.pj[OBSERVATIONS][obs]['media_file_info'].keys())[0]]['nframe'] / ( self.pj[OBSERVATIONS][obs]['media_file_info'][list(self.pj[OBSERVATIONS][obs]['media_file_info'].keys())[0]]['video_length']/1000)
                                 }
                        else:
                            self.pj[OBSERVATIONS][obs]['media_info']['fps'] = {self.pj[OBSERVATIONS][obs]['file'][PLAYER1][0]: 0}
                        self.projectChanged = True


            except:
                pass
        '''
        if project_updated:
            QMessageBox.information(self, programName, "The media files information was updated to the new project format.")


        # check program version
        memProjectChanged = self.projectChanged
        self.initialize_new_project()
        self.projectChanged = memProjectChanged
        self.load_behaviors_in_twEthogram([self.pj[ETHOGRAM][x]["code"] for x in self.pj[ETHOGRAM]])
        self.load_subjects_in_twSubjects([self.pj[SUBJECTS][x]["name"] for x in self.pj[SUBJECTS]])
        self.projectFileName = projectFileName
        self.project = True
        self.menu_options()


    def open_project_activated(self):

        # check if current observation
        if self.observationId:
            if dialog.MessageDialog(programName, "There is a current observation. What do you want to do?", ["Close observation", "Continue observation"]) == "Close observation":
                self.close_observation()
            else:
                return

        if self.projectChanged:
            response = dialog.MessageDialog(programName, "What to do about the current unsaved project?", [SAVE, DISCARD, CANCEL])

            if response == SAVE:
                if self.save_project_activated() == "not saved":
                    return

            if response == CANCEL:
                return

        if QT_VERSION_STR[0] == "4":
            fileName = QFileDialog(self).getOpenFileName(self, "Open project", "", "Project files (*.boris);;Old project files (*.obs);;All files (*)")
        else:
            fileName, _ = QFileDialog(self).getOpenFileName(self, "Open project", "", "Project files (*.boris);;Old project files (*.obs);;All files (*)")

        if fileName:
            self.open_project_json(fileName)


    def initialize_new_project(self):
        """
        initialize interface and variables for a new project
        """
        logging.info("initialize new project...")

        self.lbLogoUnito.setVisible(False)
        self.lbLogoBoris.setVisible(False)

        self.dwEthogram.setVisible(True)
        self.dwSubjects.setVisible(True)

        self.projectChanged = True


    def close_project(self):
        """
        close current project
        """

        # check if current observation
        if self.observationId:
            response = dialog.MessageDialog(programName, "There is a current observation. What do you want to do?", ["Close observation", "Continue observation"])
            if response == "Close observation":
                self.close_observation()
            if response == "Continue observation":
                return

        if self.projectChanged:
            response = dialog.MessageDialog(programName, "What to do about the current unsaved project?", [SAVE, DISCARD, CANCEL])

            if response == SAVE:
                if self.save_project_activated() == "not saved":
                    return

            if response == CANCEL:
                return

        self.dwEthogram.setVisible(False)
        self.dwSubjects.setVisible(False)

        self.projectChanged = False
        self.setWindowTitle(programName)

        self.pj = {"time_format": self.timeFormat, "project_date": "", "project_name": "", "project_description": "", "subjects_conf" : {}, "behaviors_conf": {}, "observations": {}  }
        self.project = False

        self.readConfigFile()

        self.menu_options()

        self.lbLogoUnito.setVisible(True)
        self.lbLogoBoris.setVisible(True)

        self.lbFocalSubject.setVisible(False)
        self.lbCurrentStates.setVisible(False)


    def convertTime(self, sec):
        '''
        convert time in base of current format
        return string
        '''

        if self.timeFormat == S:
            return '%.3f' % sec

        if self.timeFormat == HHMMSS:
            return seconds2time(sec)


    def edit_project(self, mode):
        """
        project management
        mode: new/edit
        """

        if self.observationId:
            QMessageBox.warning(self, programName , "Close the running observation before creating/modifying the project.")
            return

        if mode == NEW:

            if self.projectChanged:
                response = dialog.MessageDialog(programName, "What to do about the current unsaved project?", [SAVE, DISCARD, CANCEL])

                if response == SAVE:
                    self.save_project_activated()

                if response == CANCEL:
                    return

            # empty main window tables
            self.twEthogram.setRowCount(0)   # behaviors
            self.twSubjects.setRowCount(0)
            self.twEvents.setRowCount(0)


        newProjectWindow = projectDialog(logging.getLogger().getEffectiveLevel())

        if self.projectWindowGeometry:
            newProjectWindow.restoreGeometry( self.projectWindowGeometry)

        newProjectWindow.setWindowTitle(mode + " project")
        newProjectWindow.tabProject.setCurrentIndex(0)   # project information

        newProjectWindow.obs = self.pj[ETHOGRAM]
        newProjectWindow.subjects_conf = self.pj[SUBJECTS]

        if self.pj["time_format"] == S:
            newProjectWindow.rbSeconds.setChecked(True)

        if self.pj["time_format"] == HHMMSS:
            newProjectWindow.rbHMS.setChecked(True)

        if mode == NEW:

            newProjectWindow.dteDate.setDateTime(QDateTime.currentDateTime())
            newProjectWindow.lbProjectFilePath.setText("")

        if mode == EDIT:

            if self.pj["project_name"]:
                newProjectWindow.leProjectName.setText(self.pj["project_name"])

            newProjectWindow.lbProjectFilePath.setText("Project file path: " + self.projectFileName )

            if self.pj['project_description']:
                newProjectWindow.teDescription.setPlainText(self.pj["project_description"])

            if self.pj['project_date']:
                q = QDateTime.fromString(self.pj["project_date"], "yyyy-MM-ddThh:mm:ss")
                newProjectWindow.dteDate.setDateTime(q)
            else:
                newProjectWindow.dteDate.setDateTime(QDateTime.currentDateTime())


            # load subjects in editor
            if self.pj[SUBJECTS]:
                for idx in sorted_keys(self.pj[SUBJECTS]):   #   [str(x) for x in sorted([int(x) for x in self.pj[SUBJECTS].keys() ])]:
                    newProjectWindow.twSubjects.setRowCount(newProjectWindow.twSubjects.rowCount() + 1)
                    for i, field in enumerate(subjectsFields):
                        item = QTableWidgetItem(self.pj[SUBJECTS][idx][field])
                        newProjectWindow.twSubjects.setItem(newProjectWindow.twSubjects.rowCount() - 1, i ,item)

                newProjectWindow.twSubjects.resizeColumnsToContents()

            # load observation in project window
            newProjectWindow.twObservations.setRowCount(0)

            if self.pj[OBSERVATIONS]:

                for obs in sorted( self.pj[OBSERVATIONS].keys() ):

                    newProjectWindow.twObservations.setRowCount(newProjectWindow.twObservations.rowCount() + 1)

                    item = QTableWidgetItem(obs)
                    newProjectWindow.twObservations.setItem(newProjectWindow.twObservations.rowCount() - 1, 0, item)

                    item = QTableWidgetItem( self.pj[OBSERVATIONS][obs]['date'].replace('T',' ') )
                    newProjectWindow.twObservations.setItem(newProjectWindow.twObservations.rowCount() - 1, 1, item)

                    item = QTableWidgetItem( self.pj[OBSERVATIONS][obs]['description'] )
                    newProjectWindow.twObservations.setItem(newProjectWindow.twObservations.rowCount() - 1, 2, item)

                    mediaList = []
                    if self.pj[OBSERVATIONS][obs][TYPE] in [MEDIA]:
                        for idx in self.pj[OBSERVATIONS][obs][FILE]:
                            for media in self.pj[OBSERVATIONS][obs][FILE][idx]:
                                mediaList.append('#%s: %s' % (idx , media))

                    elif self.pj[OBSERVATIONS][obs][TYPE] in [LIVE]:
                        mediaList = [LIVE]

                    item = QTableWidgetItem('\n'.join( mediaList ))
                    newProjectWindow.twObservations.setItem(newProjectWindow.twObservations.rowCount() - 1, 3, item)

                newProjectWindow.twObservations.resizeColumnsToContents()

            # configuration of behaviours
            if self.pj[ETHOGRAM]:

                newProjectWindow.signalMapper = QSignalMapper(self)
                newProjectWindow.comboBoxes = []

                for i in sorted(self.pj[ETHOGRAM]):  #  [str(x) for x in sorted([int(x) for x in self.pj[ETHOGRAM].keys()])]:

                    newProjectWindow.twBehaviors.setRowCount(newProjectWindow.twBehaviors.rowCount() + 1)

                    for field in behavioursFields:

                        item = QTableWidgetItem()

                        if field == TYPE:

                            # add combobox with event type
                            newProjectWindow.comboBoxes.append(QComboBox())
                            newProjectWindow.comboBoxes[-1].addItems(BEHAVIOR_TYPES)
                            newProjectWindow.comboBoxes[-1].setCurrentIndex(BEHAVIOR_TYPES.index(self.pj[ETHOGRAM][i][field]))

                            newProjectWindow.signalMapper.setMapping(newProjectWindow.comboBoxes[-1], newProjectWindow.twBehaviors.rowCount() - 1)
                            newProjectWindow.comboBoxes[-1].currentIndexChanged["int"].connect(newProjectWindow.signalMapper.map)

                            newProjectWindow.twBehaviors.setCellWidget(newProjectWindow.twBehaviors.rowCount() - 1, behavioursFields[field], newProjectWindow.comboBoxes[-1])

                        else:
                            if field in self.pj[ETHOGRAM][i]:
                                item.setText(self.pj[ETHOGRAM][i][field])
                            else:
                                item.setText("")

                            if field in ["category", "excluded", "coding map", "modifiers"]:
                                item.setFlags(Qt.ItemIsEnabled)

                            newProjectWindow.twBehaviors.setItem(newProjectWindow.twBehaviors.rowCount() - 1, behavioursFields[field], item)

                newProjectWindow.signalMapper.mapped["int"].connect(newProjectWindow.behaviorTypeChanged)

                newProjectWindow.twBehaviors.resizeColumnsToContents()



            # load independent variables
            if INDEPENDENT_VARIABLES in self.pj:
                for i in [str(x) for x in sorted([int(x) for x in self.pj[INDEPENDENT_VARIABLES].keys()])]:
                    newProjectWindow.twVariables.setRowCount(newProjectWindow.twVariables.rowCount() + 1)

                    signalMapper = QSignalMapper(self)
                    for idx, field in enumerate(tw_indVarFields):
                        if field == "type":
                            combobox = QComboBox()
                            combobox.addItems([NUMERIC, TEXT, SET_OF_VALUES])
                            combobox.setCurrentIndex(NUMERIC_idx)
                            if self.pj[INDEPENDENT_VARIABLES][i][field] == TEXT:
                                combobox.setCurrentIndex(TEXT_idx)
                            if self.pj[INDEPENDENT_VARIABLES][i][field] == SET_OF_VALUES:
                                combobox.setCurrentIndex(SET_OF_VALUES_idx)
                            newProjectWindow.twVariables.setCellWidget(newProjectWindow.twVariables.rowCount() - 1, 2,
                                                                       combobox)
                            signalMapper.setMapping(combobox, newProjectWindow.twVariables.rowCount() - 1)
                            combobox.currentIndexChanged["int"].connect(signalMapper.map)
                            signalMapper.mapped["int"].connect(newProjectWindow.variableTypeChanged)

                        else:
                            item = QTableWidgetItem("")
                            if field in self.pj[INDEPENDENT_VARIABLES][i]:
                                item.setText(self.pj[INDEPENDENT_VARIABLES][i][field])
                            if field == "possible values":
                                item.setFlags(Qt.ItemIsEnabled)

                            newProjectWindow.twVariables.setItem(newProjectWindow.twVariables.rowCount() - 1, idx, item)

                newProjectWindow.twVariables.resizeColumnsToContents()

        newProjectWindow.dteDate.setDisplayFormat("yyyy-MM-dd hh:mm:ss")

        if mode == NEW:

            self.pj = {"time_format": HHMMSS,
                       "project_date": "",
                       "project_name": "",
                       "project_description": "",
                       SUBJECTS : {},
                       ETHOGRAM: {},
                       OBSERVATIONS: {},
                       BEHAVIORAL_CATEGORIES : [],
                       "coding_map": {}}

        # pass copy of self.pj
        newProjectWindow.pj = dict(self.pj)

        if newProjectWindow.exec_():  # button OK

            # retrieve project dict from window
            self.pj = dict(newProjectWindow.pj)

            if mode == NEW:
                self.projectFileName = ""

            self.project = True

            self.pj['project_name'] = newProjectWindow.leProjectName.text()
            self.pj['project_date'] = newProjectWindow.dteDate.dateTime().toString(Qt.ISODate)
            self.pj['project_description'] = newProjectWindow.teDescription.toPlainText()

            # time format
            if newProjectWindow.rbSeconds.isChecked():
                self.timeFormat = S

            if newProjectWindow.rbHMS.isChecked():
                self.timeFormat = HHMMSS

            self.pj['time_format'] = self.timeFormat

            # configuration
            if newProjectWindow.lbObservationsState.text() != "":
                QMessageBox.warning(self, programName, newProjectWindow.lbObservationsState.text())
            else:
                self.twEthogram.setRowCount(0)
                self.pj[ETHOGRAM] =  newProjectWindow.obs
                self.load_behaviors_in_twEthogram([self.pj[ETHOGRAM][x]["code"] for x in self.pj[ETHOGRAM]])
                self.pj[SUBJECTS] =  newProjectWindow.subjects_conf

                self.load_subjects_in_twSubjects([self.pj[SUBJECTS][x]["name"] for x in self.pj[SUBJECTS]])

                # load variables
                self.pj[INDEPENDENT_VARIABLES] =  newProjectWindow.indVar

            self.initialize_new_project()
            self.menu_options()

        self.projectWindowGeometry = newProjectWindow.saveGeometry()


    def new_project_activated(self):
        """
        new project
        """
        self.edit_project(NEW)


    def save_project_json(self, projectFileName):
        """
        save project to JSON file

        convert Decimal type in float
        """

        logging.debug("save project json {0}:".format(projectFileName))

        self.pj["project_format_version"] = project_format_version

        try:
            f = open(projectFileName, "w")
            #f.write(json.dumps(self.pj, indent=None, separators=(',', ':'), default=decimal_default))
            f.write(json.dumps(self.pj, indent=1, default=decimal_default))
            f.close()
        except:
            logging.critical("The project file can not be saved")
            QMessageBox.critical(self, programName, "The project file can not be saved!")
            return

        self.projectChanged = False


    def save_project_as_activated(self):
        """
        save current project asking for a new file name
        """
        if QT_VERSION_STR[0] == "4":
            projectNewFileName, filtr = QFileDialog(self).getSaveFileNameAndFilter(self, "Save project as", os.path.dirname(self.projectFileName), "Projects file (*.boris);;All files (*)")
        else:
            projectNewFileName, filtr = QFileDialog(self).getSaveFileName(self, "Save project as", os.path.dirname(self.projectFileName), "Projects file (*.boris);;All files (*)")
        if not projectNewFileName:
            return "Not saved"
        else:

            # add .boris if filter = 'Projects file (*.boris)'
            if  filtr == "Projects file (*.boris)" and os.path.splitext(projectNewFileName)[1] != ".boris":
                projectNewFileName += ".boris"

            self.save_project_json(projectNewFileName)
            self.projectFileName = projectNewFileName


    def save_project_activated(self):
        """
        save current project
        """
        logging.debug("Project file name: {}".format(self.projectFileName))

        if not self.projectFileName:
            if not self.pj["project_name"]:
                txt = "NONAME.boris"
            else:
                txt = self.pj['project_name'] + '.boris'
            os.chdir( os.path.expanduser("~"))
            if QT_VERSION_STR[0] == "4":
                self.projectFileName, filtr = QFileDialog(self).getSaveFileNameAndFilter(self, 'Save project', txt, 'Projects file (*.boris);;All files (*)')
            else:
                self.projectFileName, filtr = QFileDialog(self).getSaveFileName(self, 'Save project', txt, 'Projects file (*.boris);;All files (*)')

            if not self.projectFileName:
                return "not saved"

            # add .boris if filter = 'Projects file (*.boris)'
            if filtr == 'Projects file (*.boris)' and os.path.splitext(self.projectFileName)[1] != '.boris':
                self.projectFileName += '.boris'

            self.save_project_json(self.projectFileName)

        else:
            self.save_project_json(self.projectFileName)

        return ""


    def liveTimer_out(self):
        """
        timer for live observation
        """

        currentTime = self.getLaps()
        self.lbTimeLive.setText(self.convertTime(currentTime))

        # extract State events
        StateBehaviorsCodes = [self.pj[ETHOGRAM][x]['code'] for x in [y for y in self.pj[ETHOGRAM] if 'State' in self.pj[ETHOGRAM][y][TYPE]]]

        self.currentStates = {}
        # add states for no focal subject

        # TODO: replace with function (see timerout)

        self.currentStates = self.get_current_states_by_subject(StateBehaviorsCodes,
                                                                self.pj[OBSERVATIONS][self.observationId][EVENTS],
                                                                dict(self.pj[SUBJECTS], **{"": {"name": ""}}),
                                                                currentTime)

        '''
        self.currentStates[""] = []
        for sbc in StateBehaviorsCodes:
            if len([x[pj_obs_fields['code']] for x in self.pj[OBSERVATIONS][self.observationId][EVENTS ] if x[ pj_obs_fields['subject'] ] == '' and x[ pj_obs_fields['code'] ] == sbc and x[ pj_obs_fields['time'] ] <= currentTime  ] ) % 2: # test if odd
                self.currentStates[''].append(sbc)
        '''

        # add states for all configured subjects
        for idx in self.pj[SUBJECTS]:
            # add subject index
            self.currentStates[idx] = []
            for sbc in StateBehaviorsCodes:
                if len([x[pj_obs_fields['code']] for x in self.pj[OBSERVATIONS][self.observationId][EVENTS ] if x[ pj_obs_fields['subject']] == self.pj[SUBJECTS][idx]['name'] and x[ pj_obs_fields['code'] ] == sbc and x[ pj_obs_fields['time'] ] <= currentTime  ] ) % 2: # test if odd
                    self.currentStates[idx].append(sbc)

        # show current states
        if self.currentSubject:
            # get index of focal subject (by name)
            idx = [idx for idx in self.pj[SUBJECTS] if self.pj[SUBJECTS][idx]['name'] == self.currentSubject][0]
            self.lbCurrentStates.setText('%s' % (', '.join(self.currentStates[ idx ])))
        else:
            self.lbCurrentStates.setText('%s' % (', '.join(self.currentStates[ '' ])))

        # show selected subjects
        for idx in [str(x) for x in sorted([int(x) for x in self.pj[SUBJECTS].keys() ])]:
            self.twSubjects.item(int(idx), len(subjectsFields) ).setText(','.join(self.currentStates[idx]))

        # check scan sampling

        if "scan_sampling_time" in self.pj[OBSERVATIONS][self.observationId]:
            if self.pj[OBSERVATIONS][self.observationId]["scan_sampling_time"]:
                if  int(currentTime) % self.pj[OBSERVATIONS][self.observationId]["scan_sampling_time"] == 0:
                    app.beep()
                    self.liveTimer.stop()
                    self.textButton.setText("Live observation stopped (scan sampling)")





    def start_live_observation(self):
        """
        activate the live observation mode (without media file)
        """

        logging.debug("start live observation, self.liveObservationStarted: {}".format(self.liveObservationStarted))

        if "scan sampling" in self.textButton.text():
            self.textButton.setText("Stop live observation")
            self.liveTimer.start(100)
            return


        if not self.liveObservationStarted:

            if self.twEvents.rowCount():

                if dialog.MessageDialog(programName, "Delete the current events?", [YES, NO]) == YES:
                    self.twEvents.setRowCount(0)
                    self.pj[OBSERVATIONS][self.observationId][EVENTS] = []
                self.projectChanged = True
            self.textButton.setText("Stop live observation")
            self.liveStartTime = QTime()
            # set to now
            self.liveStartTime.start()
            # start timer
            self.liveTimer.start(100)
        else:

            self.textButton.setText("Start live observation")
            self.liveStartTime = None
            self.liveTimer.stop()

            if self.timeFormat == HHMMSS:
                self.lbTimeLive.setText("00:00:00.000")
            if self.timeFormat == S:
                self.lbTimeLive.setText("0.000")

        self.liveObservationStarted = not self.liveObservationStarted


    def create_subtitles(self):
        """
        create subtitles for selected observations, subjects and behaviors
        """

        result, selectedObservations = self.selectObservations(MULTIPLE)

        logging.debug("Selected observations: {0}".format(selectedObservations))

        if not selectedObservations:
            return

        plot_parameters = self.choose_obs_subj_behav_category(selectedObservations, 0)

        if not plot_parameters["selected subjects"] or not plot_parameters["selected behaviors"]:
            return

        exportDir = QFileDialog(self).getExistingDirectory(self, "Choose a directory to save subtitles", os.path.expanduser("~"), options=QFileDialog(self).ShowDirsOnly)
        if not exportDir:
            return

        cursor = self.loadEventsInDB(plot_parameters["selected subjects"], selectedObservations, plot_parameters["selected behaviors"])

        flagUnpairedEventFound = False

        for obsId in selectedObservations:

            for nplayer in [PLAYER1, PLAYER2]:

                if not self.pj[OBSERVATIONS][obsId][FILE][nplayer]:
                    continue

                duration1 = []   # in seconds
                for mediaFile in self.pj[OBSERVATIONS][obsId][FILE][nplayer]:
                    duration1.append(self.pj[OBSERVATIONS][obsId]["media_info"]["length"][mediaFile])

                subtitles = {}
                for subject in plot_parameters["selected subjects"]:

                    for behavior in plot_parameters["selected behaviors"]:

                        cursor.execute( "SELECT occurence, modifiers FROM events where observation = ? AND subject = ? AND  code = ? ORDER BY code, occurence", (obsId, subject, behavior) )
                        rows = list(cursor.fetchall() )
                        if STATE in self.eventType(behavior).upper() and len(rows) % 2:
                            #continue
                            flagUnpairedEventFound = True
                            continue

                        for idx, row in enumerate(rows):

                            mediaFileIdx = [idx1 for idx1, x in enumerate(duration1) if row["occurence"] >= sum(duration1[0:idx1])][-1]
                            if mediaFileIdx not in subtitles:
                                subtitles[mediaFileIdx] = []

                            # subtitle color
                            if subject == NO_FOCAL_SUBJECT:
                                col = "white"
                            else:
                                col = subtitlesColors[plot_parameters["selected subjects"].index(subject) % len(subtitlesColors)]

                            behaviorStr = behavior
                            if plot_parameters["include modifiers"] and row[1]:
                                behaviorStr += " ({0})".format(row[1].replace("|", ", "))

                            if POINT in self.eventType(behavior).upper():
                                laps =  "{0} --> {1}".format(seconds2time(row["occurence"]).replace(".", ","), seconds2time(row["occurence"] + 0.5).replace(".", ",") )
                                subtitles[mediaFileIdx].append( [laps, """<font color="{0}">{1}: {2}</font>""".format(col, subject, behaviorStr) ] )

                            if STATE in self.eventType(behavior).upper():
                                if idx % 2 == 0:

                                    start = seconds2time(round(row["occurence"] - sum( duration1[0:mediaFileIdx]), 3)).replace(".", ",")
                                    stop = seconds2time(round(rows[idx + 1]["occurence"] - sum( duration1[0:mediaFileIdx]), 3)).replace(".", ",")

                                    laps =  "{start} --> {stop}".format(start=start, stop=stop)
                                    subtitles[mediaFileIdx].append( [laps, """<font color="{0}">{1}: {2}</font>""".format(col, subject, behaviorStr) ] )


                try:
                    for mediaIdx in subtitles:
                        subtitles[mediaIdx].sort()
                        with open( "{exportDir}{sep}{fileName}.srt".format(exportDir=exportDir, sep=os.sep, fileName=os.path.basename(self.pj[OBSERVATIONS][obsId][FILE][nplayer][mediaIdx])), "w") as f:
                            for idx, sub in enumerate(subtitles[mediaIdx]):
                                f.write("{0}{3}{1}{3}{2}{3}{3}".format(idx + 1, sub[0], sub[1], "\n"))
                except:
                    errorMsg = sys.exc_info()[1]
                    logging.critical(errorMsg)
                    QMessageBox.critical(None, programName, str(errorMsg), QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)


        self.statusbar.showMessage("Subtitles file(s) created in {} directory".format(exportDir), 0)


    def export_aggregated_events(self):
        """
        export aggregated events in SQL (sql) or Tabular format (tsv, csv, xls, ods, html)
        format is selected using the filename extension
        """

        result, selectedObservations = self.selectObservations(MULTIPLE)

        if not selectedObservations:
            return

        plot_parameters = self.choose_obs_subj_behav_category(selectedObservations, maxTime=0, flagShowIncludeModifiers=False, flagShowExcludeBehaviorsWoEvents=False)

        if not plot_parameters["selected subjects"] or not plot_parameters["selected behaviors"]:
            return

        includeMediaInfo = None
        for obsId in selectedObservations:
            if self.pj[OBSERVATIONS][obsId][TYPE] in [MEDIA]:
                includeMediaInfo = YES
                break

        fileFormats = ("Tab Separated Values (*.txt *.tsv);;"
                       "Comma Separated Values (*.txt *.csv);;"
                       "Microsoft Excel XLS (*.xls);;"
                       "Open Document Spreadsheet ODS (*.ods);;"
                       "HTML (*.html);;"
                       "SDIS (*.sds);;"
                       "SQL dump file file (*.sql);;"
                       "All files (*)")
        while True:
            if QT_VERSION_STR[0] == "4":
                fileName, filter_ = QFileDialog(self).getSaveFileNameAndFilter(self, "Export aggregated events", "", fileFormats)
            else:
                fileName, filter_ = QFileDialog(self).getSaveFileName(self, "Export aggregated events", "", fileFormats)

            if not fileName:
                return

            outputFormat = ""
            availableFormats = ("tsv", "csv", "xls", "ods", "html", "sql", "sds")
            for fileExtension in availableFormats:
                if fileExtension in filter_:
                    outputFormat = fileExtension
                    if not fileName.upper().endswith("." + fileExtension.upper()):
                        fileName += "." + fileExtension

            if not outputFormat:
                QMessageBox.warning(self, programName, "Choose a file format", QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)
            else:
                break


        if not outputFormat:
            QMessageBox.warning(self, programName, "The file extension must be in {}".format(" ".join(availableFormats)), QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)
            return

        if outputFormat == "sql":
            out = "CREATE TABLE events (id INTEGER PRIMARY KEY ASC, observation TEXT, date DATE, media_file TEXT, subject TEXT, behavior TEXT, modifiers TEXT, event_type TEXT, start FLOAT, stop FLOAT, comment_start TEXT, comment_stop TEXT);" + "\n"
            out += "BEGIN TRANSACTION;\n"
            template = """INSERT INTO events (observation, date, media_file, subject, behavior, modifiers, event_type, start, stop, comment_start, comment_stop) VALUES ("{observation}","{date}", "{media_file}", "{subject}", "{behavior}","{modifiers}","{event_type}",{start},{stop},"{comment_start}","{comment_stop}");\n"""

        else:
            data = tablib.Dataset()
            data.title = "Aggregated events"
            header = ["Observation id", "Observation date", "Media file", "Total media length", "FPS"]

            # independent variables
            if "independent_variables" in self.pj:
                for idx in sorted(self.pj["independent_variables"].keys()):
                    header.append(self.pj["independent_variables"][idx]["label"])

            header.extend(["Subject", "Behavior", "Modifiers", "Behavior type", "Start", "Stop", "Comment start", "Comment stop"])

            data.append(header)

        self.statusbar.showMessage("Exporting aggregated events in {} format".format(outputFormat.upper()), 0)
        flagUnpairedEventFound = False

        for obsId in selectedObservations:

            duration1 = []   # in seconds
            if self.pj[OBSERVATIONS][obsId]["type"] in [MEDIA]:
                try:
                    for mediaFile in self.pj[OBSERVATIONS][obsId][FILE][PLAYER1]:
                        if "media_info" in self.pj[OBSERVATIONS][obsId]:
                            duration1.append(self.pj[OBSERVATIONS][obsId]["media_info"]["length"][mediaFile])
                        else:
                            #if "media_file_info" in
                            print("no media_info tag")
                except:
                    print("error")
                    pass

            cursor = self.loadEventsInDB(plot_parameters["selected subjects"], selectedObservations, plot_parameters["selected behaviors"])

            for subject in plot_parameters["selected subjects"]:

                for behavior in plot_parameters["selected behaviors"]:

                    cursor.execute("SELECT occurence, modifiers, comment FROM events WHERE observation = ? AND subject = ? AND code = ? ORDER by occurence", (obsId, subject, behavior))
                    rows = list(cursor.fetchall())

                    if STATE in self.eventType(behavior).upper() and len(rows) % 2:  # unpaired events
                        flagUnpairedEventFound = True
                        continue

                    for idx, row in enumerate(rows):

                        if self.pj[OBSERVATIONS][obsId]["type"] in [MEDIA]:

                            print("duration1", duration1)
                            print([idx1 for idx1, x in enumerate(duration1) if row["occurence"] >= sum(duration1[0:idx1])])

                            mediaFileIdx = [idx1 for idx1, x in enumerate(duration1) if row["occurence"] >= sum(duration1[0:idx1])][-1]
                            mediaFileString = self.pj[OBSERVATIONS][obsId][FILE][PLAYER1][mediaFileIdx]
                            fpsString = self.pj[OBSERVATIONS][obsId]["media_info"]["fps"][self.pj[OBSERVATIONS][obsId][FILE][PLAYER1][mediaFileIdx]]
                        else:
                            mediaFileString = "LIVE"
                            fpsString = "NA"

                        if POINT in self.eventType(behavior).upper():

                            if outputFormat == "sql":
                                out += template.format(observation=obsId,
                                                    date=self.pj[OBSERVATIONS][obsId]["date"].replace("T", " "),
                                                    media_file=mediaFileString,
                                                    total_length=sum(duration1),
                                                    fps=fpsString,
                                                    subject=subject,
                                                    behavior=behavior,
                                                    modifiers=row["modifiers"].strip(),
                                                    event_type=POINT,
                                                    start=row["occurence"],
                                                    stop=0,
                                                    comment_start=row["comment"],
                                                    comment_stop="")
                            else:
                                row_data = []
                                row_data.extend([obsId,
                                            self.pj[OBSERVATIONS][obsId]["date"].replace("T", " "),
                                            mediaFileString,
                                            sum(duration1),
                                            fpsString])

                                # independent variables
                                if "independent_variables" in self.pj:
                                    for idx_var in sorted(self.pj["independent_variables"].keys()):
                                        if self.pj["independent_variables"][idx_var]["label"] in self.pj[OBSERVATIONS][obsId]["independent_variables"]:
                                           row_data.append(self.pj[OBSERVATIONS][obsId]["independent_variables"][self.pj["independent_variables"][idx_var]["label"]])
                                        else:
                                            row_data.append("")

                                row_data.extend([subject,
                                            behavior,
                                            row["modifiers"].strip(),
                                            POINT,
                                            row["occurence"],
                                            0,
                                            row["comment"],
                                            ""
                                            ])
                                data.append(row_data)


                        if STATE in self.eventType(behavior).upper():
                            if idx % 2 == 0:
                                if outputFormat == "sql":
                                    out += template.format(observation=obsId,
                                                        date=self.pj[OBSERVATIONS][obsId]["date"].replace("T", " "),
                                                        media_file=mediaFileString,
                                                        total_length=sum(duration1),
                                                        fps=fpsString,
                                                        subject=subject,
                                                        behavior=behavior,
                                                        modifiers=row["modifiers"].strip(),
                                                        event_type=STATE,
                                                        start=row["occurence"],
                                                        stop=rows[idx + 1]["occurence"],
                                                        comment_start=row["comment"],
                                                        comment_stop=rows[idx + 1]["comment"])

                                else:
                                    row_data = []

                                    row_data.extend([obsId,
                                            self.pj[OBSERVATIONS][obsId]["date"].replace("T", " "),
                                            mediaFileString,
                                            sum(duration1),
                                            fpsString])

                                    # independent variables
                                    if "independent_variables" in self.pj:
                                        for idx_var in sorted(self.pj["independent_variables"].keys()):
                                            if self.pj["independent_variables"][idx_var]["label"] in self.pj[OBSERVATIONS][obsId]["independent_variables"]:
                                               row_data.append(self.pj[OBSERVATIONS][obsId]["independent_variables"][self.pj["independent_variables"][idx_var]["label"]])
                                            else:
                                                row_data.append("")

                                    row_data.extend([subject,
                                            behavior,
                                            row["modifiers"].strip(),
                                            STATE,
                                            row["occurence"],
                                            rows[idx + 1]["occurence"],
                                            row["comment"],
                                            rows[idx + 1]["comment"]
                                            ])

                                    data.append(row_data)


        if outputFormat == "sql":
            out += "END TRANSACTION;\n"
            try:
                with open(fileName, "w") as f:
                    f.write(out)
            except:
                errorMsg = sys.exc_info()[1]
                logging.critical(errorMsg)
                QMessageBox.critical(None, programName, str(errorMsg), QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)

        elif outputFormat == "sds": # SDIS format

            out = "% SDIS file created by BORIS (www.boris.unito.it) at {}\nTimed <seconds>;\n".format(datetime_iso8601())

            for obsId in selectedObservations:
                # observation id
                out += "\n<{}>\n".format(obsId)

                dataList = list(data[1:])
                for event in sorted(dataList, key=lambda x: x[-4]):  # sort events by start time

                    #print(event)
                    if event[0] == obsId:

                        behavior = event[-7]
                        # replace various char by _
                        for char in [" ", "-", "/"]:
                            behavior = behavior.replace(char, "_")

                        subject = event[-8]
                        # replace various char by _
                        for char in [" ", "-", "/"]:
                            subject = subject.replace(char, "_")


                        event_start = "{0:.3f}".format(round(event[-4], 3))  # start event (from end for independent variables)

                        if not event[-3]:  # stop event (from end)
                            event_stop = "{0:.3f}".format(round(event[-4] + 0.001, 3))
                        else:
                            event_stop = "{0:.3f}".format(round(event[-3], 3))
                        out += "{subject}_{behavior},{start}-{stop} ".format(subject=subject, behavior=behavior, start=event_start, stop=event_stop)

                out += "/\n\n"

            with open(fileName, "wb") as f:
                f.write(str.encode(out))

        else:
            if outputFormat == "tsv":
                with open(fileName, "wb") as f:
                    f.write(str.encode(data.tsv))
            if outputFormat == "csv":
                with open(fileName, "wb") as f:
                    f.write(str.encode(data.csv))
            if outputFormat == "html":
                with open(fileName, "wb") as f:
                    f.write(str.encode(data.html))
            if outputFormat == "ods":
                with open(fileName, "wb") as f:
                    f.write(data.ods)
            if outputFormat == "xls":
                with open(fileName, "wb") as f:
                    f.write(data.xls)

        if flagUnpairedEventFound:
            QMessageBox.warning(self, programName, "Some state events are not paired. They were excluded from export",
                    QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)

        self.statusbar.showMessage("Aggregated events exported successfully", 0)



    def export_state_events_as_textgrid(self):
        """
        export state events as Praat textgrid
        """

        result, selectedObservations = self.selectObservations(MULTIPLE)

        if not selectedObservations:
            return

        plot_parameters = self.choose_obs_subj_behav_category(selectedObservations, maxTime=0, flagShowIncludeModifiers=False, flagShowExcludeBehaviorsWoEvents=False)

        if not plot_parameters["selected subjects"] or not plot_parameters["selected behaviors"]:
            return

        exportDir = QFileDialog(self).getExistingDirectory(self, "Export events as TextGrid", os.path.expanduser('~'), options=QFileDialog(self).ShowDirsOnly)
        if not exportDir:
            return

        self.statusbar.showMessage("Exporting events as TextGrid", 0)

        for obsId in selectedObservations:

            out = """File type = "ooTextFile"
Object class = "TextGrid"

xmin = 0
xmax = 98.38814058956916
tiers? <exists>
size = {subjectNum}
item []:
"""
            subjectheader = """    item [{subjectIdx}]:
        class = "IntervalTier"
        name = "{subject}"
        xmin = {intervalsMin}
        xmax = {intervalsMax}
        intervals: size = {intervalsSize}
"""

            template = """        intervals [{count}]:
            xmin = {xmin}
            xmax = {xmax}
            text = "{name}"
"""

            flagUnpairedEventFound = False
            totalMediaDuration = round(self.observationTotalMediaLength(obsId), 3)
            cursor = self.loadEventsInDB(plot_parameters["selected subjects"], selectedObservations, plot_parameters["selected behaviors"])
            cursor.execute( "SELECT count(distinct subject) FROM events WHERE observation = '{}' AND subject in ('{}') AND type = 'STATE' ".format(obsId, "','".join(plot_parameters["selected subjects"])))
            subjectsNum = int(list(cursor.fetchall())[0][0])

            subjectsMin, subjectsMax = 0, totalMediaDuration

            out = """File type = "ooTextFile"
Object class = "TextGrid"

xmin = {subjectsMin}
xmax = {subjectsMax}
tiers? <exists>
size = {subjectsNum}
item []:
""".format(subjectsNum=subjectsNum, subjectsMin=subjectsMin, subjectsMax=subjectsMax)

            subjectIdx = 0
            for subject in plot_parameters["selected subjects"]:

                subjectIdx += 1

                cursor.execute("SELECT count(*) FROM events WHERE observation = ? AND subject = ? AND type = 'STATE' ", (obsId, subject))
                intervalsSize = int(list(cursor.fetchall())[0][0] / 2)

                intervalsMin, intervalsMax = 0, totalMediaDuration

                out += subjectheader

                cursor.execute("SELECT occurence, code FROM events WHERE observation = ? AND subject = ? AND type = 'STATE' order by occurence", (obsId, subject))

                rows = [{"occurence": float2decimal(r["occurence"]), "code": r["code"]}  for r in cursor.fetchall()]
                if not rows:
                    continue

                count = 0

                # check if 1st behavior starts at the beginning

                if rows[0]["occurence"] > 0:
                    count += 1
                    out += template.format(count=count, name="null", xmin=0.0, xmax=rows[0]["occurence"])

                for idx, row in enumerate(rows):
                    if idx % 2 == 0:

                        # check if events not interlacced
                        if row["code"] != rows[idx + 1]["code"]:
                            QMessageBox.critical(None, programName, "The events are interlaced. It is not possible to produce the Praat TextGrid file", QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)
                            return

                        count += 1
                        out += template.format(count=count, name=row["code"], xmin=row["occurence"], xmax=rows[idx + 1]["occurence"] )

                        # check if difference is > 0.001
                        if len(rows) > idx + 2:
                            if rows[idx + 2]["occurence"] - rows[idx + 1]["occurence"] > 0.001:

                                logging.debug( type(rows[idx + 2]["occurence"]) )

                                logging.debug("difference: {}-{}={}".format( rows[idx + 2]["occurence"], rows[idx + 1]["occurence"], rows[idx + 2]["occurence"] - rows[idx + 1]["occurence"] ))

                                out += template.format(count=count + 1, name="null", xmin=rows[idx + 1]["occurence"], xmax=rows[idx + 2]["occurence"] )
                                count += 1
                            else:
                                logging.debug("difference <=0.001: {} - {} = {}".format( rows[idx + 2]["occurence"], rows[idx + 1]["occurence"], rows[idx + 2]["occurence"] - rows[idx + 1]["occurence"] ))
                                rows[idx + 2]["occurence"] = rows[idx + 1]["occurence"]
                                logging.debug("difference after: {} - {} = {}".format( rows[idx + 2]["occurence"], rows[idx + 1]["occurence"], rows[idx + 2]["occurence"] - rows[idx + 1]["occurence"] ))

                # check if last event ends at the end of media file
                if rows[-1]["occurence"] < self.observationTotalMediaLength(obsId):
                    count += 1
                    out += template.format(count=count, name="null", xmin=rows[-1]["occurence"], xmax=totalMediaDuration )

                # add info
                out = out.format(subjectIdx=subjectIdx, subject=subject, intervalsSize=count, intervalsMin=intervalsMin, intervalsMax=intervalsMax)


            try:
                with open( "{exportDir}{sep}{obsId}.textGrid".format( exportDir=exportDir, sep=os.sep, obsId=obsId ), "w") as f:
                    f.write(out)

                if flagUnpairedEventFound:
                    QMessageBox.warning(self, programName, "Some state events are not paired. They were excluded from export",\
                            QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)

                self.statusbar.showMessage("Events exported successfully", 10000)

            except:
                errorMsg = sys.exc_info()[1]
                logging.critical(errorMsg)
                QMessageBox.critical(None, programName, str(errorMsg), QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)



    def media_file_info(self):
        """
        show info about media file (current media file if observation opened)
        """

        def info_from_ffmpeg(media_file_path):
            if os.path.isfile(media_file_path):
                out =  "<b>{}</b><br><br>".format(os.path.basename(media_file_path))

                out += "File size: {} Mb<br>".format(round(os.stat(media_file_path).st_size / 1024 / 1024, 1))

                ffmpeg_output = subprocess.getoutput('"{}" -i "{}"'.format(ffmpeg_bin, media_file_path)).split("Stream #0")
                if len(ffmpeg_output) > 1:
                    out += "{}<br>".format(ffmpeg_output[1])
                if len(ffmpeg_output) > 2:
                    out += "{}<br>".format(ffmpeg_output[2].replace("At least one output file must be specified", ""))
            else:
                out = ""
            return out



        if self.observationId and self.playerType == VLC:

            media = self.mediaplayer.get_media()

            logging.info("State: {}".format(self.mediaplayer.get_state()))
            logging.info("Media (get_mrl): {}".format(bytes_to_str(media.get_mrl())))
            logging.info("media.get_meta(0): {}".format(media.get_meta(0)))
            logging.info("Track: {}/{}".format(self.mediaplayer.video_get_track(), self.mediaplayer.video_get_track_count()))
            logging.info("number of media in media list: {}".format(self.media_list.count()))
            logging.info("get time: {}  duration: {}".format(self.mediaplayer.get_time(), media.get_duration()))
            logging.info("Position: {} %".format(self.mediaplayer.get_position()))
            logging.info("FPS: {}".format(self.mediaplayer.get_fps()))
            logging.info("Rate: {}".format(self.mediaplayer.get_rate()))
            logging.info("Video size: {}".format(self.mediaplayer.video_get_size(0)))
            logging.info("Scale: {}".format(self.mediaplayer.video_get_scale()))
            logging.info("Aspect ratio: {}".format(self.mediaplayer.video_get_aspect_ratio()))
            logging.info("is seekable? {0}".format(self.mediaplayer.is_seekable()))
            logging.info("has_vout? {0}".format(self.mediaplayer.has_vout()))

            out = ""
            for idx in self.pj[OBSERVATIONS][self.observationId][FILE]:
                for file_ in self.pj[OBSERVATIONS][self.observationId][FILE][idx]:
                    out += info_from_ffmpeg(file_)


            QMessageBox.about(self, programName + " - Media file information", "{}<br><br>Total duration: {} s".format(out, self.convertTime(sum(self.duration)/1000)))

        else:

            if QT_VERSION_STR[0] == "4":
                fileName = QFileDialog(self).getOpenFileName(self, "Select a media file to re-encode/resize", "", "Media files (*)")
            else:
                fileName, _ = QFileDialog(self).getOpenFileName(self, "Select a media file to re-encode/resize", "", "Media files (*)")

            if fileName:
                QMessageBox.about(self, programName + " - Media file information", "{}<br>".format(info_from_ffmpeg(fileName)))



    def switch_playing_mode(self):
        """
        switch between frame mode and VLC mode
        triggered by frame by frame button and toolbox item change
        """

        if self.playerType != VLC:
            return

        if self.playMode == FFMPEG:  # return to VLC mode

            self.playMode = VLC

            if hasattr(self, "frame_viewer1"):
                self.frame_viewer1_mem_geometry = self.frame_viewer1.geometry()
                del self.frame_viewer1
            if self.second_player():
                if hasattr(self, "frame_viewer2"):
                    self.frame_viewer2_mem_geometry = self.frame_viewer2.geometry()
                    del self.frame_viewer2

            globalCurrentTime = int(self.FFmpegGlobalFrame * (1000 / list(self.fps.values())[0]))

            # set on media player end
            currentMediaTime = int(sum(self.duration))

            for idx, media in enumerate(self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER1]):
                if globalCurrentTime < sum(self.duration[0:idx + 1]):
                    self.mediaListPlayer.play_item_at_index(idx)
                    while True:
                        if self.mediaListPlayer.get_state() in [vlc.State.Playing, vlc.State.Ended]:
                            break
                    self.mediaListPlayer.pause()
                    currentMediaTime = int(globalCurrentTime - sum(self.duration[0:idx]))
                    break

            self.mediaplayer.set_time(currentMediaTime)

            if self.second_player():

                # set on media player2 end
                currentMediaTime2 = int(sum(self.duration2))
                globalCurrentTime2 = int(self.FFmpegGlobalFrame2 * (1000 / list(self.fps2.values())[0]))
                for idx, media in enumerate(self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER2]):
                    if globalCurrentTime2 < sum(self.duration2[0:idx + 1]):
                        self.mediaListPlayer2.play_item_at_index(idx)
                        while True:
                            if self.mediaListPlayer2.get_state() in [vlc.State.Playing, vlc.State.Ended]:
                                break
                        self.mediaListPlayer2.pause()
                        currentMediaTime2 = int(globalCurrentTime2 - sum(self.duration2[0:idx]))
                        break
                self.mediaplayer2.set_time(currentMediaTime2)

            self.toolBox.setCurrentIndex(VIDEO_TAB)

            self.FFmpegTimer.stop()

            logging.info("ffmpeg timer stopped")

            # set thread for cleaning temp directory
            if self.ffmpeg_cache_dir_max_size:
                self.cleaningThread.exiting = True

        # go to frame by frame mode
        else:

            if list(self.fps.values())[0] == 0:
                logging.warning("The frame per second value is not available. Frame-by-frame mode will not be available")
                QMessageBox.critical(None, programName, "The frame per second value is not available. Frame-by-frame mode will not be available",
                    QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)
                self.actionFrame_by_frame.setChecked(False)
                return

            if len(set(self.fps.values())) != 1:
                logging.warning("The frame-by-frame mode will not be available because the video files have different frame rates")
                QMessageBox.warning(self, programName, ("The frame-by-frame mode will not be available"
                                                        " because the video files have different frame rates ({}).".format(
                                                         ", ".join([str(i) for i in list(self.fps.values())]))),
                    QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)
                self.actionFrame_by_frame.setChecked(False)
                return

            # check if FPS media player 1 != FPS media player 2
            if self.second_player():
                if list(self.fps.values())[0] != list(self.fps2.values())[0]:
                    logging.warning("The frame-by-frame mode will not be available because the video files in player #1 and player #2 have different frame rates")
                    QMessageBox.warning(self, programName, ("The frame-by-frame mode will not be available"
                                                            " because the video files have different frame rates ({} and {} FPS).".format(
                                                             list(self.fps.values())[0], list(self.fps2.values())[0])),
                                         QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)
                    self.actionFrame_by_frame.setChecked(False)
                    return

            self.pause_video()
            self.playMode = FFMPEG

            # check temp dir for images from ffmpeg
            if not self.ffmpeg_cache_dir:
                self.imageDirectory = tempfile.gettempdir()
            else:
                self.imageDirectory = self.ffmpeg_cache_dir

            # load list of images in a set
            if not self.imagesList:
                self.imagesList.update([f.replace(self.imageDirectory + os.sep, "").split("_")[0] for f in glob.glob(self.imageDirectory + os.sep + "BORIS@*")])

            # show frame-by_frame tab
            self.toolBox.setCurrentIndex(1)

            print("self.mediaplayer.get_time()", self.mediaplayer.get_time())
            globalTime = (sum(self.duration[0: self.media_list.index_of_item(self.mediaplayer.get_media())]) + self.mediaplayer.get_time())

            fps = list(self.fps.values())[0]

            globalCurrentFrame = round(globalTime / (1000/fps))

            self.FFmpegGlobalFrame = globalCurrentFrame

            if self.second_player():
                globalTime2 = (sum(self.duration2[0 : self.media_list2.index_of_item(self.mediaplayer2.get_media())]) + self.mediaplayer2.get_time())
                globalCurrentFrame2 = round(globalTime2 / (1000/fps))
                self.FFmpegGlobalFrame2 = globalCurrentFrame2

            if self.FFmpegGlobalFrame > 0:
                self.FFmpegGlobalFrame -= 1
            if self.second_player():
                if self.FFmpegGlobalFrame2 > 0:
                    self.FFmpegGlobalFrame2 -= 1

            self.ffmpegTimerOut()

            # set thread for cleaning temp directory
            if self.ffmpeg_cache_dir_max_size:
                self.cleaningThread.exiting = False
                self.cleaningThread.ffmpeg_cache_dir_max_size = self.ffmpeg_cache_dir_max_size * 1024 * 1024
                self.cleaningThread.tempdir = self.imageDirectory + os.sep
                self.cleaningThread.start()


        # enable/disable speed button
        self.actionNormalSpeed.setEnabled(self.playMode == VLC)
        self.actionFaster.setEnabled(self.playMode == VLC)
        self.actionSlower.setEnabled(self.playMode == VLC)

        logging.info("new play mode: {0}".format(self.playMode))

        self.menu_options()


    def snapshot(self):
        """
        take snapshot of current video
        snapshot is saved on media path
        """

        if self.pj[OBSERVATIONS][self.observationId][TYPE] in [MEDIA]:

            if self.playerType == VLC:

                if self.playMode == FFMPEG:

                    for idx, media in enumerate(self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER1]):
                        if self.FFmpegGlobalFrame < sum(self.duration[0:idx + 1]):
                            dirName, fileName = os.path.split(media)
                            snapshotFilePath = dirName + os.sep + os.path.splitext(fileName)[0] + "_" + str(self.FFmpegGlobalFrame) + ".png"
                            if self.detachFrameViewer or self.second_player():
                                self.frame_viewer1.lbFrame.pixmap().save(snapshotFilePath)
                            elif not self.detachFrameViewer:
                                self.lbFFmpeg.pixmap().save(snapshotFilePath)
                            self.statusbar.showMessage("Snapshot player #1 saved in {}".format(snapshotFilePath), 0)
                            break

                    if self.second_player():
                        for idx, media in enumerate(self.pj[OBSERVATIONS][self.observationId][FILE][PLAYER2]):
                            if self.FFmpegGlobalFrame2 < sum(self.duration2[0:idx + 1]):
                                dirName, fileName = os.path.split(media)
                                snapshotFilePath = dirName + os.sep + os.path.splitext(fileName)[0] + "_" + str(self.FFmpegGlobalFrame2) + ".png"
                                self.frame_viewer2.lbFrame.pixmap().save(snapshotFilePath)
                                self.statusbar.showMessage("Snapshot player #2 saved in {}".format(snapshotFilePath), 0)
                                break

                else:  # VLC

                    current_media_path = url2path(self.mediaplayer.get_media().get_mrl())
                    dirName, fileName = os.path.split(current_media_path)
                    self.mediaplayer.video_take_snapshot(0, "{dirName}{sep}{fileNameWOExt}_{time}.png".format(
                                                              dirName=dirName,
                                                              sep=os.sep,
                                                              fileNameWOExt=os.path.splitext(fileName)[0],
                                                              time=self.mediaplayer.get_time())
                                                         , 0, 0)

                    # check if multi mode
                    # second video together
                    if self.simultaneousMedia:

                        current_media_path = url2path(self.mediaplayer2.get_media().get_mrl())

                        dirName, fileName = os.path.split( current_media_path )
                        self.mediaplayer2.video_take_snapshot(0, "{dirName}{sep}{fileNameWOExt}_{time}.png".format(
                                                              dirName=dirName,
                                                              sep=os.sep,
                                                              fileNameWOExt=os.path.splitext(fileName)[0],
                                                              time=self.mediaplayer2.get_time())
                                                              , 0, 0)


    def video_zoom(self, player, zoom_value):
        """
        change video zoom
        """
        try:
            if player == 1:
                self.mediaplayer.video_set_scale(zoom_value)
            if player == 2 and self.simultaneousMedia:
                self.mediaplayer2.video_set_scale(zoom_value)
        except:
            pass

        try:
            if player == 1:
                zv = self.mediaplayer.video_get_scale()
                self.actionZoom1_fitwindow.setChecked(zv == 0)
                self.actionZoom1_1_1.setChecked(zv == 1)
                self.actionZoom1_1_2.setChecked(zv == 0.5)
                self.actionZoom1_1_4.setChecked(zv == 0.25)
                self.actionZoom1_2_1.setChecked(zv == 2)
            if player == 2 and self.simultaneousMedia:
                zv = self.mediaplayer2.video_get_scale()
                self.actionZoom2_fitwindow.setChecked(zv == 0)
                self.actionZoom2_1_1.setChecked(zv == 1)
                self.actionZoom2_1_2.setChecked(zv == 0.5)
                self.actionZoom2_1_4.setChecked(zv == 0.25)
                self.actionZoom2_2_1.setChecked(zv == 2)

        except:
            pass




    def video_normalspeed_activated(self):
        """
        set playing speed at normal speed
        """

        if self.playerType == VLC and self.playMode == VLC:
            self.play_rate = 1
            self.mediaplayer.set_rate(self.play_rate)
            # second video together
            if self.simultaneousMedia:
                self.mediaplayer2.set_rate(self.play_rate)
            self.lbSpeed.setText('x{:.3f}'.format(self.play_rate))
            logging.info('play rate: {:.3f}'.format(self.play_rate))


    def video_faster_activated(self):
        """
        increase playing speed by play_rate_step value
        """

        if self.playerType == VLC and self.playMode == VLC:

            if self.play_rate + self.play_rate_step <= 8:
                self.play_rate += self.play_rate_step
                self.mediaplayer.set_rate(self.play_rate)

                # second video together
                if self.simultaneousMedia:
                    self.mediaplayer2.set_rate(self.play_rate)
                self.lbSpeed.setText('x{:.3f}'.format(self.play_rate))

            logging.info('play rate: {:.3f}'.format(self.play_rate))

    def video_slower_activated(self):
        """
        decrease playing speed by play_rate_step value
        """

        if self.playerType == VLC and self.playMode == VLC:

            if self.play_rate - self.play_rate_step >= 0.1:
                self.play_rate -= self.play_rate_step
                self.mediaplayer.set_rate(self.play_rate)

                # second video together
                if self.simultaneousMedia:
                    self.mediaplayer2.set_rate(self.play_rate)

                self.lbSpeed.setText('x{:.3f}'.format(self.play_rate))

            logging.info('play rate: {:.3f}'.format(self.play_rate))




    def add_event(self):
        """
        manually add event to observation
        """

        if not self.observationId:
            self.no_observation()
            return

        laps = self.getLaps()

        if not self.pj[ETHOGRAM]:
            QMessageBox.warning(self, programName, "The ethogram is not set!")
            return

        editWindow = DlgEditEvent(logging.getLogger().getEffectiveLevel())
        editWindow.setWindowTitle("Add a new event")

        # send pj to edit_event window
        editWindow.pj = self.pj

        if self.timeFormat == HHMMSS:
            editWindow.dsbTime.setVisible(False)
            editWindow.teTime.setTime(QtCore.QTime.fromString(seconds2time(laps), HHMMSSZZZ) )

        if self.timeFormat == S:
            editWindow.teTime.setVisible(False)
            editWindow.dsbTime.setValue(float(laps))

        sortedSubjects = [""] + sorted([self.pj[SUBJECTS][x]["name"] for x in self.pj[SUBJECTS]])

        editWindow.cobSubject.addItems(sortedSubjects)

        sortedCodes = sorted([self.pj[ETHOGRAM][x]['code'] for x in self.pj[ETHOGRAM]])

        editWindow.cobCode.addItems(sortedCodes)

        # activate signal
        #editWindow.cobCode.currentIndexChanged.connect(editWindow.codeChanged)

        editWindow.currentModifier = ""

        if editWindow.exec_():  #button OK

            if self.timeFormat == HHMMSS:
                newTime = time2seconds(editWindow.teTime.time().toString(HHMMSSZZZ))

            if self.timeFormat == S:
                newTime = Decimal(editWindow.dsbTime.value())

            """memTime = newTime"""

            # get modifier(s)
            # check mod type (QPushButton or QDialog)
            '''
            if type(editWindow.mod)  is select_modifiers.ModifiersRadioButton:
                modifiers = editWindow.mod.getModifiers()

                if len(modifiers) == 1:
                    modifier_str = modifiers[0]
                    if modifier_str == 'None':
                        modifier_str = ''
                else:
                    modifier_str = '|'.join( modifiers )

            #QPushButton coding map
            if type(editWindow.mod)  is QPushButton:
                modifier_str = editWindow.mod.text().split('\n')[1].replace('Area(s): ','')
            '''

            for obs_idx in self.pj[ETHOGRAM]:
                if self.pj[ETHOGRAM][obs_idx]['code'] == editWindow.cobCode.currentText():

                    event = self.full_event(obs_idx)

                    event['subject'] = editWindow.cobSubject.currentText()
                    if editWindow.leComment.toPlainText():
                        event['comment'] = editWindow.leComment.toPlainText()

                    self.writeEvent(event, newTime)
                    break


    def edit_event(self):
        """
        edit each event items from the selected row
        """
        if not self.observationId:
            self.no_observation()
            return

        if self.twEvents.selectedItems():

            editWindow = DlgEditEvent(logging.getLogger().getEffectiveLevel())
            editWindow.setWindowTitle("Edit event parameters")

            # pass project to window
            editWindow.pj = self.pj
            editWindow.currentModifier = ""

            row = self.twEvents.selectedItems()[0].row()

            if self.timeFormat == HHMMSS:
                editWindow.dsbTime.setVisible(False)
                editWindow.teTime.setTime(QtCore.QTime.fromString(seconds2time( self.pj[OBSERVATIONS][self.observationId][EVENTS][row][ 0 ] ), "hh:mm:ss.zzz") )

            if self.timeFormat == S:
                editWindow.teTime.setVisible(False)
                editWindow.dsbTime.setValue(self.pj[OBSERVATIONS][self.observationId][EVENTS][row][0])

            sortedSubjects = [""] + sorted([self.pj[SUBJECTS][x]["name"] for x in self.pj[SUBJECTS]])

            editWindow.cobSubject.addItems(sortedSubjects)

            if self.pj[OBSERVATIONS][self.observationId][EVENTS][row][EVENT_SUBJECT_FIELD_IDX] in sortedSubjects:
                editWindow.cobSubject.setCurrentIndex( sortedSubjects.index( self.pj[OBSERVATIONS][self.observationId][EVENTS][row][EVENT_SUBJECT_FIELD_IDX]))
            else:
                QMessageBox.warning(self, programName, "The subject <b>{}</b> do not exists more in the subject's list".format(self.pj[OBSERVATIONS][self.observationId][EVENTS][row][EVENT_SUBJECT_FIELD_IDX]))
                editWindow.cobSubject.setCurrentIndex(0)

            sortedCodes = sorted([self.pj[ETHOGRAM][x]["code"] for x in self.pj[ETHOGRAM]])

            editWindow.cobCode.addItems(sortedCodes)

            # check if selected code is in code's list (no modification of codes)
            if self.pj[OBSERVATIONS][self.observationId][EVENTS][row][EVENT_BEHAVIOR_FIELD_IDX] in sortedCodes:
                editWindow.cobCode.setCurrentIndex( sortedCodes.index( self.pj[OBSERVATIONS][self.observationId][EVENTS][row][EVENT_BEHAVIOR_FIELD_IDX] ) )
            else:
                logging.warning("The behaviour <b>{0}</b> do not exists more in the ethogram".format(self.pj[OBSERVATIONS][self.observationId][EVENTS][row][EVENT_BEHAVIOR_FIELD_IDX] ) )
                QMessageBox.warning(self, programName, "The behaviour <b>{}</b> do not exists more in the ethogram".format(self.pj[OBSERVATIONS][self.observationId][EVENTS][row][EVENT_BEHAVIOR_FIELD_IDX]))
                editWindow.cobCode.setCurrentIndex(0)

            logging.debug("original modifiers: {}".format(self.pj[OBSERVATIONS][self.observationId][EVENTS][row][EVENT_MODIFIER_FIELD_IDX]))

            # comment
            editWindow.leComment.setPlainText( self.pj[OBSERVATIONS][self.observationId][EVENTS][row][EVENT_COMMENT_FIELD_IDX])

            if editWindow.exec_():  #button OK

                self.projectChanged = True

                if self.timeFormat == HHMMSS:
                    newTime = time2seconds(editWindow.teTime.time().toString(HHMMSSZZZ))

                if self.timeFormat == S:
                    newTime = Decimal(str(editWindow.dsbTime.value()))

                for key in self.pj[ETHOGRAM]:
                    if self.pj[ETHOGRAM][key]["code"] == editWindow.cobCode.currentText():
                        event = self.full_event(key)
                        event["subject"] = editWindow.cobSubject.currentText()
                        event["comment"] = editWindow.leComment.toPlainText()
                        event["row"] = row
                        event["original_modifiers"] = self.pj[OBSERVATIONS][self.observationId][EVENTS][row][pj_obs_fields['modifier']]
                        print("edited",event)

                        self.writeEvent(event, newTime)
                        break

        else:
            QMessageBox.warning(self, programName, "Select an event to edit")


    def no_media(self):
        QMessageBox.warning(self, programName, "There is no media available")


    def no_project(self):
        QMessageBox.warning(self, programName, "There is no project")


    def no_observation(self):
        QMessageBox.warning(self, programName, "There is no current observation")


    def twEthogram_doubleClicked(self):
        '''
        add event by double-clicking in ethogram list
        '''
        if self.observationId:
            if self.twEthogram.selectedIndexes():

                ethogramRow = self.twEthogram.selectedIndexes()[0].row()

                logging.debug('ethogram row: {0}'.format(ethogramRow  ))
                logging.debug(self.pj[ETHOGRAM][str(ethogramRow)])

                code = self.twEthogram.item(ethogramRow, 1).text()

                event = self.full_event(str(ethogramRow))

                logging.debug('event: {0}'.format(event))

                self.writeEvent(event, self.getLaps())

        else:
            self.no_observation()



    def actionUser_guide_triggered(self):
        """
        open user guide URL if it exists otherwise open user guide URL
        """
        userGuideFile = os.path.dirname(os.path.realpath(__file__)) + "/boris_user_guide.pdf"
        if os.path.isfile(userGuideFile) :
            if sys.platform.startswith("linux"):
                subprocess.call(["xdg-open", userGuideFile])
            else:
                os.startfile(userGuideFile)
        else:
            QDesktopServices.openUrl(QUrl("http://boris.readthedocs.org"))

    def actionHow_to_cite_BORIS_activated(self):
        """
        display dialog with how to cite BORIS
        """
        self.results = dialog.ResultsWidget()
        self.results.setWindowTitle("How to cite BORIS")
        self.results.ptText.clear()
        self.results.ptText.appendHtml(("Friard, O. and Gamba, M. (2016), "
                                        "BORIS: a free, versatile open-source event-logging software for video/audio coding and live observations."
                                        " Methods Ecol Evol, 7: 1325–1330. doi:10.1111/2041-210X.12584"))
        self.results.show()


    def actionAbout_activated(self):
        """
        about dialog
        """

        ver = 'v. {0}'.format(__version__)

        players = []
        players.append("VLC media player v. {}".format(bytes_to_str(vlc.libvlc_get_version())))
        players.append("VLC libraries path: {}".format(vlc.plugin_path))
        players.append("FFmpeg path: {}".format(self.ffmpeg_bin))


        QMessageBox.about(self, "About " + programName, """<b>{prog_name}</b> {ver} - {date}
        <p>Copyright &copy; 2012-2017 Olivier Friard - Marco Gamba<br>
        Department of Life Sciences and Systems Biology<br>
        University of Torino - Italy<br>
        <br>
        BORIS is released under the <a href="http://www.gnu.org/copyleft/gpl.html">GNU General Public License</a><br>
        <br>
        The authors would like to acknowledge Sergio Castellano, Valentina Matteucci and Laura Ozella for their precious help.<br>
        <br>
        See <a href="http://www.boris.unito.it">www.boris.unito.it</a> for more details.<br>
        <p>Python {python_ver} ({architecture}) - Qt {qt_ver} - PyQt{pyqt_ver} on {system}<br>
        CPU type: {cpu_info}<br>
        <br>
        {players}""".format(prog_name=programName,
                            ver=ver,
                            date=__version_date__,
                            python_ver=platform.python_version(),
                            architecture="64-bit" if sys.maxsize > 2**32 else "32-bit",
                            pyqt_ver=PYQT_VERSION_STR,
                            system=platform.system(),
                            qt_ver=QT_VERSION_STR,
                            cpu_info=platform.machine(),
                            players="<br>".join(players)))


    def hsVideo_sliderMoved(self):
        """
        media position slider moved
        adjust media position
        """

        if self.pj[OBSERVATIONS][self.observationId][TYPE] in [MEDIA]:

            if self.playerType == VLC and self.playMode == VLC:
                sliderPos = self.hsVideo.value() / (slider_maximum - 1)
                videoPosition = sliderPos * self.mediaplayer.get_length()
                self.mediaplayer.set_time(int(videoPosition))
                # second video together
                if self.simultaneousMedia:
                    # synchronize 2nd player
                    self.mediaplayer2.set_time( int(self.mediaplayer.get_time() - self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] * 1000))
                self.timer_out(scrollSlider=False)
                self.timer_spectro_out()


    def get_events_current_row(self):
        """
        get events current row corresponding to video/frame-by-frame position
        paint twEvents with tracking cursor
        scroll to corresponding event
        """

        global ROW

        if self.pj[OBSERVATIONS][self.observationId][EVENTS]:
            ct = self.getLaps()
            if ct >= self.pj[OBSERVATIONS][self.observationId][EVENTS][-1][0]:
                ROW = len( self.pj[OBSERVATIONS][self.observationId][EVENTS] )
            else:
                cr_list =  [idx for idx, x in enumerate(self.pj[OBSERVATIONS][self.observationId][EVENTS][:-1]) if x[0] <= ct and self.pj[OBSERVATIONS][self.observationId][EVENTS][idx+1][0] > ct ]

                if cr_list:
                    ROW = cr_list[0]
                    if not self.trackingCursorAboveEvent:
                        ROW +=  1
                else:
                    ROW = -1

            self.twEvents.setItemDelegate(StyledItemDelegateTriangle(self.twEvents))
            self.twEvents.scrollToItem(self.twEvents.item(ROW, 0))

    def get_current_states_by_subject(self, stateBehaviorsCodes, events, subjects, time):
        """
        get current states for subjects at given time

        """
        currentStates = {}
        for idx in subjects:
            currentStates[idx] = []
            for sbc in stateBehaviorsCodes:
                if len([x[ EVENT_BEHAVIOR_FIELD_IDX ] for x in events
                                                       if x[EVENT_SUBJECT_FIELD_IDX] == subjects[idx]["name"]
                                                          and x[EVENT_BEHAVIOR_FIELD_IDX] == sbc
                                                          and x[EVENT_TIME_FIELD_IDX] <= time]) % 2: # test if odd
                    currentStates[idx].append(sbc)
        return currentStates



    def timer_out(self, scrollSlider=True):
        """
        indicate the video current position and total length for VLC player
        scroll video slider to video position
        Time offset is NOT added!
        triggered by timer
        """

        if not self.observationId:
            return

        if self.pj[OBSERVATIONS][self.observationId][TYPE] in [MEDIA]:

            # cumulative time
            currentTime = self.getLaps() * 1000

            if self.beep_every:
                if currentTime % (self.beep_every*1000) <= 300:
                    self.beep(" -f 555 -l 460")

            # current media time
            try:
                mediaTime = self.mediaplayer.get_time()
            except:
                return

            # highlight current event in tw events and scroll event list
            self.get_events_current_row()

            # check if second video
            if self.simultaneousMedia:

                if TIME_OFFSET_SECOND_PLAYER in self.pj[OBSERVATIONS][self.observationId]:

                    # sync 2nd player on 1st player when no offset
                    if self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] == 0:
                        t1, t2 = self.mediaplayer.get_time(), self.mediaplayer2.get_time()
                        if abs(t1 - t2) >= 300:
                            self.mediaplayer2.set_time(t1)

                    if self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] > 0:

                        if mediaTime < self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] * 1000:

                            if self.mediaListPlayer2.get_state() == vlc.State.Playing:
                                self.mediaplayer2.set_time(0)
                                self.mediaListPlayer2.pause()
                        else:
                            if self.mediaListPlayer.get_state() == vlc.State.Playing:
                                t1, t2 = self.mediaplayer.get_time(), self.mediaplayer2.get_time()
                                if abs((t1 - t2) - self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] * 1000) >= 300:  # synchr if diff >= 300 ms
                                    self.mediaplayer2.set_time(int(t1 - self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] * 1000))
                                self.mediaListPlayer2.play()

                    if self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] < 0:
                        mediaTime2 = self.mediaplayer2.get_time()

                        if mediaTime2 < abs(self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] * 1000):

                            if self.mediaListPlayer.get_state() == vlc.State.Playing:
                                print("p1 paused")
                                self.mediaplayer.set_time(0)
                                self.mediaListPlayer.pause()
                        else:
                            if self.mediaListPlayer2.get_state() == vlc.State.Playing:
                                t1, t2 = self.mediaplayer.get_time(), self.mediaplayer2.get_time()
                                if abs((t2-t1) + self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] * 1000) >= 300 :  # synchr if diff >= 300 ms
                                    self.mediaplayer.set_time( int(t2 + self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] * 1000) )
                                self.mediaListPlayer.play()

            currentTimeOffset = Decimal(currentTime / 1000) + Decimal(self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET])

            totalGlobalTime = sum(self.duration)

            mediaName = ""

            if self.mediaplayer.get_length():

                self.mediaTotalLength = self.mediaplayer.get_length() / 1000

                # current state(s)

                # extract State events
                StateBehaviorsCodes = [self.pj[ETHOGRAM][x]["code"] for x in [y for y in self.pj[ETHOGRAM] if STATE in self.pj[ETHOGRAM][y][TYPE].upper()]]

                self.currentStates = {}

                # add current states for all subject and for "no focal subject"

                self.currentStates = self.get_current_states_by_subject(StateBehaviorsCodes, self.pj[OBSERVATIONS][self.observationId][EVENTS], dict(self.pj[SUBJECTS], **{"": {"name": ""}}), currentTimeOffset)

                # show current subject
                cm = {}
                if self.currentSubject:
                    # get index of focal subject (by name)
                    idx = [idx for idx in self.pj[SUBJECTS] if self.pj[SUBJECTS][idx]["name"] == self.currentSubject][0]
                else:
                    idx = ""

                # show current state(s)
                txt = []
                for cs in self.currentStates[idx]:
                    for ev in self.pj[OBSERVATIONS][self.observationId][EVENTS]:
                        if ev[EVENT_TIME_FIELD_IDX] > currentTimeOffset:
                            break
                        if ev[EVENT_SUBJECT_FIELD_IDX] == self.currentSubject:
                            if ev[EVENT_BEHAVIOR_FIELD_IDX] == cs:
                                cm[cs] = ev[EVENT_MODIFIER_FIELD_IDX]
                    # state and modifiers (if any)
                    txt.append(cs + " ({}) ".format(cm[cs])*(cm[cs] != ""))

                txt = ", ".join(txt)

                self.lbCurrentStates.setText(re.sub(" \(.*\)", "", txt))

                # show current states in subjects table
                for idx in [str(x) for x in sorted([int(x) for x in self.pj[SUBJECTS].keys()])]:
                    self.twSubjects.item(int(idx), len(subjectsFields)).setText(",".join(self.currentStates[idx]))

                mediaName = self.mediaplayer.get_media().get_meta(0)

                # update status bar
                msg = ""
                if self.mediaListPlayer.get_state() == vlc.State.Playing or self.mediaListPlayer.get_state() == vlc.State.Paused:
                    msg = "{media_name}: <b>{time} / {total_time}</b>".format(media_name=mediaName,
                                                                              time=self.convertTime(Decimal(mediaTime / 1000)),
                                                                              total_time=self.convertTime(Decimal(self.mediaTotalLength)))

                    if self.media_list.count() > 1:
                        msg += " | total: <b>%s / %s</b>" % ((self.convertTime(Decimal(currentTime / 1000)),
                                                               self.convertTime(Decimal(totalGlobalTime / 1000))))
                    if self.mediaListPlayer.get_state() == vlc.State.Paused:
                        msg += " (paused)"

                if msg:
                    # show time on status bar
                    self.lbTime.setText(msg)

                    # set video scroll bar
                    if scrollSlider:
                        self.hsVideo.setValue(mediaTime / self.mediaplayer.get_length() * (slider_maximum - 1))
            else:
                self.statusbar.showMessage("Media length not available now", 0)

            if (self.memMedia and mediaName != self.memMedia) or (self.mediaListPlayer.get_state() == vlc.State.Ended and self.timer.isActive()):

                if CLOSE_BEHAVIORS_BETWEEN_VIDEOS in self.pj[OBSERVATIONS][self.observationId] and self.pj[OBSERVATIONS][self.observationId][CLOSE_BEHAVIORS_BETWEEN_VIDEOS]:

                    logging.debug("video changed")
                    logging.debug("current states: {}".format( self.currentStates))

                    for subjIdx in self.currentStates:

                        if subjIdx:
                            subjName = self.pj[SUBJECTS][subjIdx]["name"]
                        else:
                            subjName = ""

                        for behav in self.currentStates[subjIdx]:

                            cm = ""
                            for ev in self.pj[OBSERVATIONS][self.observationId][EVENTS]:
                                if ev[EVENT_TIME_FIELD_IDX] > currentTime / 1000:  # time
                                    break

                                if ev[EVENT_SUBJECT_FIELD_IDX] == subjName:  # current subject name
                                    if ev[EVENT_BEHAVIOR_FIELD_IDX] == behav:   # code
                                        cm = ev[EVENT_MODIFIER_FIELD_IDX]

                            #self.pj[OBSERVATIONS][self.observationId][EVENTS].append([currentTime / 1000 - Decimal('0.001'), subjName, behav, cm, ''] )

                            event = {"subject": subjName, "code": behav, "modifiers": cm, "comment": "", "excluded": ""}

                            self.writeEvent(event, currentTime / 1000 - Decimal("0.001"))

                            #self.loadEventsInTW(self.observationId)

            self.memMedia = mediaName

            if self.mediaListPlayer.get_state() == vlc.State.Ended:
                self.timer.stop()




    def load_behaviors_in_twEthogram(self, behaviorsToShow):
        """
        fill ethogram table with ethogram from pj
        """

        self.twEthogram.setRowCount(0)
        if self.pj[ETHOGRAM]:
            for idx in sorted_keys(self.pj[ETHOGRAM]):   #    [str(x) for x in sorted([int(x) for x in self.pj[ETHOGRAM].keys()])]:
                if self.pj[ETHOGRAM][idx]["code"] in behaviorsToShow:
                    self.twEthogram.setRowCount(self.twEthogram.rowCount() + 1)
                    for col, field in enumerate(["key", "code", "type", "description", "modifiers", "excluded"]):
                        self.twEthogram.setItem(self.twEthogram.rowCount() - 1, col, QTableWidgetItem(self.pj[ETHOGRAM][idx][field]))

        if self.twEthogram.rowCount() < len(self.pj[ETHOGRAM].keys()):
            self.dwEthogram.setWindowTitle("Ethogram (filtered {0}/{1})".format(self.twEthogram.rowCount(), len(self.pj[ETHOGRAM].keys())))

            if self.observationId:
                self.pj[OBSERVATIONS][self.observationId]["filtered behaviors"] = behaviorsToShow
        else:
            self.dwEthogram.setWindowTitle("Ethogram")


    def load_subjects_in_twSubjects(self, subjects_to_show):
        """
        fill subjects table widget with subjects from subjects_to_show
        """

        self.twSubjects.setRowCount(0)
        if self.pj[SUBJECTS]:
            for idx in sorted_keys(self.pj[SUBJECTS]):
                if self.pj[SUBJECTS][idx]["name"] in subjects_to_show:

                    self.twSubjects.setRowCount(self.twSubjects.rowCount() + 1)

                    for idx2, field in enumerate(subjectsFields):
                        self.twSubjects.setItem(self.twSubjects.rowCount() - 1, idx2, QTableWidgetItem(self.pj[SUBJECTS][idx][field]))

                    # add cell for current state(s) after last subject field
                    self.twSubjects.setItem(self.twSubjects.rowCount() - 1, len(subjectsFields) , QTableWidgetItem(""))



    def update_events_start_stop(self):
        """
        update status start/stop of events in Events table
        take consideration of subject and modifiers

        do not return value
        """

        stateEventsList = [self.pj[ETHOGRAM][x][BEHAVIOR_CODE] for x in self.pj[ETHOGRAM] if STATE in self.pj[ETHOGRAM][x][TYPE].upper()]

        for row in range(0, self.twEvents.rowCount()):

            t = self.twEvents.item(row, tw_obs_fields["time"]).text()

            if ":" in t:
                time = time2seconds(t)
            else:
                time = Decimal(t)

            subject = self.twEvents.item(row, tw_obs_fields["subject"]).text()
            code = self.twEvents.item(row, tw_obs_fields["code"]).text()
            modifier = self.twEvents.item(row, tw_obs_fields["modifier"]).text()

            # check if code is state
            if code in stateEventsList:
                # how many code before with same subject?
                nbEvents = len([event[EVENT_BEHAVIOR_FIELD_IDX] for event in self.pj[OBSERVATIONS][self.observationId][EVENTS]
                                                                  if event[EVENT_BEHAVIOR_FIELD_IDX] == code
                                                                     and event[EVENT_TIME_FIELD_IDX] < time
                                                                     and event[EVENT_SUBJECT_FIELD_IDX] == subject
                                                                     and event[EVENT_MODIFIER_FIELD_IDX] == modifier])

                if nbEvents and (nbEvents % 2): # test >0 and  odd
                    self.twEvents.item(row, tw_obs_fields[TYPE]).setText(STOP)
                else:
                    self.twEvents.item(row, tw_obs_fields[TYPE]).setText(START)


    def update_events_start_stop2(self, events):
        """
        returns events with status (START/STOP or POINT)
        take consideration of subject
        """

        stateEventsList = [self.pj[ETHOGRAM][x][BEHAVIOR_CODE] for x in self.pj[ETHOGRAM] if STATE in self.pj[ETHOGRAM][x][TYPE].upper()]
        eventsFlagged = []
        for event in events:
            time, subject, code, modifier = event[EVENT_TIME_FIELD_IDX], event[EVENT_SUBJECT_FIELD_IDX], event[EVENT_BEHAVIOR_FIELD_IDX], event[EVENT_MODIFIER_FIELD_IDX]
            # check if code is state
            if code in stateEventsList:
                # how many code before with same subject?
                if len([x[EVENT_BEHAVIOR_FIELD_IDX] for x in events
                                                     if x[EVENT_BEHAVIOR_FIELD_IDX] == code
                                                        and x[EVENT_TIME_FIELD_IDX] < time
                                                        and x[EVENT_SUBJECT_FIELD_IDX] == subject
                                                        and x[EVENT_MODIFIER_FIELD_IDX] == modifier]) % 2: # test if odd
                    flag = STOP
                else:
                    flag = START
            else:
                flag = POINT

            eventsFlagged.append(event + [flag])

        return eventsFlagged


    def checkSameEvent(self, obsId, time, subject, code ):
        """
        check if a same event is already in events list (time, subject, code)
        """
        return [time, subject, code] in [[x[EVENT_TIME_FIELD_IDX], x[EVENT_SUBJECT_FIELD_IDX], x[EVENT_BEHAVIOR_FIELD_IDX]] for x in self.pj[OBSERVATIONS][obsId][EVENTS]]


    def writeEvent(self, event, memTime):
        """
        add event from pressed key to observation
        offset is added to event time
        ask for modifiers if configured
        load events in tableview
        scroll to active event
        """

        logging.debug("write event - event: {0}  memtime: {1}".format(event, memTime))

        if event is None:
            return

        # add time offset if not from editing
        if "row" not in event:
            memTime += Decimal(self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET]).quantize(Decimal(".001"))

        # check if a same event is already in events list (time, subject, code)
        # "row" present in case of event editing

        if "row" not in event and self.checkSameEvent(self.observationId, memTime, self.currentSubject, event["code"]):
            _ = dialog.MessageDialog(programName, "The same event already exists (same time, behavior code and subject).", [OK])
            return

        if not "from map" in event:   # modifiers only for behaviors without coding map
            # check if event has modifiers
            modifier_str = ""

            if event["modifiers"]:

                # pause media
                if self.pj[OBSERVATIONS][self.observationId][TYPE] in [MEDIA]:

                    if self.playerType == VLC:
                        if self.playMode == FFMPEG:
                            memState = self.FFmpegTimer.isActive()
                            if memState:
                                self.pause_video()
                        else:
                            memState = self.mediaListPlayer.get_state()
                            if memState == vlc.State.Playing:
                                self.pause_video()

                modifiersList = []
                if "|" in event["modifiers"]:
                    for modifiersString in event["modifiers"].split("|"):
                        modifiersList.append([s.strip() for s in modifiersString.split(",")])
                else:
                    modifiersList.append([s.strip() for s in event["modifiers"].split(",")])

                # check if editing (original_modifiers key)
                currentModifiers = event["original_modifiers"] if "original_modifiers" in event else ""

                modifierSelector = select_modifiers.ModifiersList(event["code"], modifiersList, currentModifiers)

                if modifierSelector.exec_():
                    modifiers = modifierSelector.getModifiers()
                    if len(modifiers) == 1:
                        modifier_str = modifiers[0]
                        if modifier_str == "None":
                            modifier_str = ""
                    else:
                        modifier_str = "|".join(modifiers)
                else:
                    if currentModifiers: # editing
                        modifier_str = currentModifiers
                    else:
                        return

                # restart media
                if self.pj[OBSERVATIONS][self.observationId][TYPE] in [MEDIA]:

                    if self.playerType == VLC:

                        if self.playMode == FFMPEG:
                            if memState:
                                self.play_video()
                        else:

                            if memState == vlc.State.Playing:
                                self.play_video()

        else:
            modifier_str = event["from map"]

        # update current state
        if "row" not in event: # no editing
            if self.currentSubject:
                csj = []
                for idx in self.currentStates:
                    if idx in self.pj[SUBJECTS] and self.pj[SUBJECTS][idx]["name"] == self.currentSubject:
                        csj = self.currentStates[idx]
                        break

            else:  # no focal subject
                try:
                    csj = self.currentStates[""]
                except:
                    csj = []

            cm = {} # modifiers for current behaviors
            for cs in csj:
                for ev in self.pj[OBSERVATIONS][self.observationId][EVENTS]:
                    if ev[EVENT_TIME_FIELD_IDX] > memTime:
                        break

                    if ev[EVENT_SUBJECT_FIELD_IDX] == self.currentSubject:
                        if ev[EVENT_BEHAVIOR_FIELD_IDX] == cs:
                            cm[cs] = ev[EVENT_MODIFIER_FIELD_IDX]

            '''
            print("csj",csj)
            print("cm",cm)
            print("modifier_str", modifier_str)
            print("modifier_str", modifier_str.replace("None", "").replace("|", ""))
            '''

            for cs in csj:
                '''print("cs", cs)'''

                # close state if same state without modifier
                if self.close_the_same_current_event and (event["code"] == cs) and modifier_str.replace("None", "").replace("|", "") == "":
                    modifier_str = cm[cs]
                    continue

                if (event["excluded"] and cs in event["excluded"].split(",")) or (event["code"] == cs and cm[cs] != modifier_str):
                    # add excluded state event to observations (= STOP them)
                    self.pj[OBSERVATIONS][self.observationId][EVENTS].append([memTime - Decimal("0.001"), self.currentSubject, cs, cm[cs], ""])


        # remove key code from modifiers
        modifier_str = re.sub(" \(.*\)", "", modifier_str)

        if "comment" in event:
            comment = event["comment"]
        else:
            comment = ""

        if "subject" in event:
            subject = event["subject"]
        else:
            subject = self.currentSubject

        # add event to pj
        if "row" in event:
            self.pj[OBSERVATIONS][self.observationId][EVENTS][event["row"]] = [memTime, subject, event["code"], modifier_str, comment]
        else:
            self.pj[OBSERVATIONS][self.observationId][EVENTS].append([memTime, subject, event["code"], modifier_str, comment])

        # sort events in pj
        self.pj[OBSERVATIONS][self.observationId][EVENTS].sort()

        # reload all events in tw
        self.loadEventsInTW(self.observationId)

        item = self.twEvents.item([i for i, t in enumerate( self.pj[OBSERVATIONS][self.observationId][EVENTS]) if t[0] == memTime][0], 0)

        self.twEvents.scrollToItem(item)

        self.projectChanged = True




    def fill_lwDetailed(self, obs_key, memLaps):
        """
        fill listwidget with all events coded by key
        return index of behaviour
        """

        # check if key duplicated
        items = []
        for idx in self.pj[ETHOGRAM]:
            if self.pj[ETHOGRAM][idx]["key"] == obs_key:

                code_descr = self.pj[ETHOGRAM][idx]["code"]
                if  self.pj[ETHOGRAM][idx]["description"]:
                    code_descr += " - " + self.pj[ETHOGRAM][idx]["description"]
                items.append(code_descr)
                self.detailedObs[code_descr] = idx

        items.sort()

        dbc = dialog.DuplicateBehaviorCode("The <b>{}</b> key codes more behaviors.<br>Choose the correct one:".format(obs_key), items)
        if dbc.exec_():
            code = dbc.getCode()
            if code:
                return self.detailedObs[code]
            else:
                return None

        '''
        item, ok = QInputDialog.getItem(self, programName, "The <b>{}</b> key codes more behaviors.<br>Choose the correct one:".format(obs_key), items, 0, False)
        if ok and item:
            obs_idx = self.detailedObs[item]
            return obs_idx
        else:
            return None
        '''


    def getLaps(self):
        """
        return cumulative laps time from begining of observation
        as Decimal in seconds
        no more add time offset!
        """

        if self.pj[OBSERVATIONS][self.observationId]["type"] in [LIVE]:

            if self.liveObservationStarted:
                now = QTime()
                now.start()  # current time
                memLaps = Decimal(str(round( self.liveStartTime.msecsTo(now) / 1000, 3)))
                return memLaps
            else:
                return Decimal("0.0")

        if self.pj[OBSERVATIONS][self.observationId]["type"] in [MEDIA]:

            if self.playerType == VIEWER:
                return Decimal(0)

            if self.playerType == VLC:

                if self.playMode == FFMPEG:
                    # cumulative time

                    memLaps = Decimal( self.FFmpegGlobalFrame * ( 1000 / list(self.fps.values())[0]) / 1000).quantize(Decimal(".001"))

                    return memLaps

                else: # playMode == VLC

                    # cumulative time
                    memLaps = Decimal(str(round(( sum(self.duration[0 : self.media_list.index_of_item(self.mediaplayer.get_media()) ]) \
                              + self.mediaplayer.get_time()) / 1000 , 3)))

                    return memLaps


    def full_event(self, obs_idx):
        """
        ask modifiers from coding if configured and add them under 'from map' key
        """

        event = dict(self.pj[ETHOGRAM][obs_idx])
        # check if coding map
        if "coding map" in self.pj[ETHOGRAM][obs_idx] and self.pj[ETHOGRAM][obs_idx]["coding map"]:

            # pause if media and media playing
            if self.pj[OBSERVATIONS][self.observationId][TYPE] in [MEDIA]:
                if self.playerType == VLC:
                    memState = self.mediaListPlayer.get_state()
                    if memState == vlc.State.Playing:
                        self.pause_video()

            self.codingMapWindow = modifiers_coding_map.ModifiersCodingMapWindowClass(self.pj["coding_map"][self.pj[ETHOGRAM][obs_idx]["coding map"]])

            self.codingMapWindow.resize(640, 640)
            if self.codingMapWindowGeometry:
                 self.codingMapWindow.restoreGeometry(self.codingMapWindowGeometry)

            if not self.codingMapWindow.exec_():
                return

            self.codingMapWindowGeometry = self.codingMapWindow.saveGeometry()

            event["from map"] = self.codingMapWindow.getCodes()

            # restart media
            if self.pj[OBSERVATIONS][self.observationId][TYPE] in [MEDIA]:

                if self.playerType == VLC:
                    if memState == vlc.State.Playing:
                        self.play_video()

        return event

    def frame_backward(self):
        """
        go one frame back
        """

        if self.playMode == FFMPEG:
            logging.debug("current frame {0}".format( self.FFmpegGlobalFrame ))
            if self.FFmpegGlobalFrame > 1:
                self.FFmpegGlobalFrame -= 2
                #newTime = 1000 * self.FFmpegGlobalFrame / list(self.fps.values())[0]
                self.ffmpegTimerOut()
                logging.debug("new frame {0}".format(self.FFmpegGlobalFrame))

    def frame_forward(self):
        """
        go one frame forward
        """
        if self.playMode == FFMPEG:
            self.ffmpegTimerOut()

    def beep(self, parameters):
        """
        emit beep on various platform
        """
        if sys.platform.startswith("linux"):
            os.system("beep {}".format(parameters))
        else:
            app.beep()


    def keyPressEvent(self, event):

        logging.debug("text #{0}#  event key: {1} ".format(event.text(), event.key() ))

        '''
        if (event.modifiers() & Qt.ShiftModifier):   # SHIFT

        QApplication.keyboardModifiers()

        http://qt-project.org/doc/qt-5.0/qtcore/qt.html#Key-enum

        ESC: 16777216
        '''


        if self.playMode == VLC:
            self.timer_out()

        if not self.observationId:
            return

        # beep
        if self.confirmSound:
            self.beep("")

        if self.playerType == VLC and self.mediaListPlayer.get_state() != vlc.State.Paused:
            flagPlayerPlaying = True
        else:
            flagPlayerPlaying = False


        # check if media ever played

        if self.playerType == VLC:
            if self.mediaListPlayer.get_state() == vlc.State.NothingSpecial:
                return

        ek = event.key()
        #if ek == Qt.Key_Enter and event.text():

        logging.debug("key event {0}".format(ek))


        if ek in [16777248, 16777249, 16777217, 16781571]: # shift tab ctrl
            return


        if self.playerType == VIEWER:
            QMessageBox.critical(self, programName, "The current observation is opened in VIEW mode.\nIt is not allowed to log events in this mode.")
            return


        if ek == Qt.Key_Escape:  #16777216:
            self.switch_playing_mode()
            return

        # play / pause with space bar
        if ek == Qt.Key_Space and self.pj[OBSERVATIONS][self.observationId][TYPE] in [MEDIA]:
            if self.mediaListPlayer.get_state() != vlc.State.Paused:
                self.pause_video()
            else:
                self.play_video()
            return

        # frame-by-frame mode
        if self.playMode == FFMPEG:
            if ek == 47 or ek == Qt.Key_Left:   # /   one frame back

                logging.debug("current frame {0}".format( self.FFmpegGlobalFrame))
                if self.FFmpegGlobalFrame > 1:
                    self.FFmpegGlobalFrame -= 2
                    newTime = 1000 * self.FFmpegGlobalFrame / list(self.fps.values())[0]
                    self.ffmpegTimerOut()
                    logging.debug("new frame {0}".format(self.FFmpegGlobalFrame))
                return

            if ek == 42 or ek == Qt.Key_Right:  # *  read next frame

                logging.debug("(next) current frame {0}".format( self.FFmpegGlobalFrame))
                self.ffmpegTimerOut()
                logging.debug("(next) new frame {0}".format( self.FFmpegGlobalFrame))
                return


        if self.playerType == VLC:
            #  jump backward
            if ek == Qt.Key_Down:
                logging.debug("jump backward")
                self.jumpBackward_activated()
                return

            # jump forward
            if ek == Qt.Key_Up:
                logging.debug("jump forward")
                self.jumpForward_activated()
                return

            # next media file (page up)
            if ek == Qt.Key_PageUp:
                logging.debug("next media file")
                self.next_media_file()

            # previous media file (page down)
            if ek == Qt.Key_PageDown:
                logging.debug("previous media file")
                self.previous_media_file()


        if not self.pj[ETHOGRAM]:
            QMessageBox.warning(self, programName, "The ethogram is not configured")
            return

        obs_key = None

        # check if key is function key
        if (ek in function_keys):
            if function_keys[ek] in [self.pj[ETHOGRAM][x]["key"] for x in self.pj[ETHOGRAM]]:
                obs_key = function_keys[ek]

        # get video time


        if (self.pj[OBSERVATIONS][self.observationId][TYPE] in [LIVE]
           and "scan_sampling_time" in self.pj[OBSERVATIONS][self.observationId]
           and self.pj[OBSERVATIONS][self.observationId]["scan_sampling_time"]):
            if self.timeFormat == HHMMSS:
                memLaps = Decimal(int(time2seconds(self.lbTimeLive.text())))
            if self.timeFormat == S:
                memLaps = Decimal(int(Decimal(self.lbTimeLive.text())))

        else:
            memLaps = self.getLaps()

        if memLaps == None:
            return

        if ((ek in range(33, 256)) and (ek not in [Qt.Key_Plus, Qt.Key_Minus])) or (ek in function_keys) or (ek == Qt.Key_Enter and event.text()):

            obs_idx, subj_idx  = -1, -1
            count = 0

            if (ek in function_keys):
                ek_unichr = function_keys[ek]
            elif ek != Qt.Key_Enter:
                ek_unichr = chr(ek)

            if ek == Qt.Key_Enter and event.text():
                ek_unichr = ""
                for o in self.pj[ETHOGRAM]:
                    if self.pj[ETHOGRAM][o]["code"] == event.text():
                        obs_idx = o
                        count += 1
            else:
                # count key occurence in ethogram
                for o in self.pj[ETHOGRAM]:
                    if self.pj[ETHOGRAM][o]["key"] == ek_unichr:
                        obs_idx = o
                        count += 1

            # check if key defines a suject
            flag_subject = False
            for idx in self.pj[SUBJECTS]:
                if ek_unichr == self.pj[SUBJECTS][idx]["key"]:
                    subj_idx = idx

            # select between code and subject
            if subj_idx != -1 and count:

                if self.playerType == VLC:
                    if self.mediaListPlayer.get_state() != vlc.State.Paused:
                        flagPlayerPlaying = True
                        self.pause_video()


                r = dialog.MessageDialog(programName, "This key defines a behavior and a subject. Choose one", ["&Behavior", "&Subject"])
                if r == "&Subject":
                    count = 0
                if r == "&Behavior":
                    subj_idx = -1


            # check if key codes more events
            if subj_idx == -1 and count > 1:
                if self.pj[OBSERVATIONS][self.observationId][TYPE] in [MEDIA]:
                    if self.playerType == VLC:
                        if self.mediaListPlayer.get_state() != vlc.State.Paused:
                            flagPlayerPlaying = True
                            self.pause_video()

                # let user choose event
                obs_idx = self.fill_lwDetailed(ek_unichr, memLaps)

                logging.debug("obs_idx: {}".format(obs_idx))

                if obs_idx:
                    count = 1

            if self.playerType == VLC and flagPlayerPlaying:
                self.play_video()

            if count == 1:
                # check if focal subject is defined
                if not self.currentSubject and self.alertNoFocalSubject:
                    if self.pj[OBSERVATIONS][self.observationId][TYPE] in [MEDIA]:
                        if self.playerType == VLC:
                            if self.mediaListPlayer.get_state() != vlc.State.Paused:
                                flagPlayerPlaying = True
                                self.pause_video()

                    response = dialog.MessageDialog(programName, ("The focal subject is not defined. Do you want to continue?\n"
                                                                  "Use Preferences menu option to modify this behaviour."), [YES, NO])

                    if self.pj[OBSERVATIONS][self.observationId][TYPE] in [MEDIA] and flagPlayerPlaying:
                        self.play_video()

                    if response == NO:
                        return

                event = self.full_event(obs_idx)

                self.writeEvent(event, memLaps)

            elif count == 0:

                # check if key defines a suject
                flag_subject = False
                for idx in self.pj[SUBJECTS]:
                    if ek_unichr == self.pj[SUBJECTS][idx]["key"]:
                        flag_subject = True

                        # select or deselect current subject
                        if self.currentSubject == self.pj[SUBJECTS][idx]["name"]:
                            self.deselectSubject()
                        else:
                            self.selectSubject( self.pj[SUBJECTS][idx]["name"])

                if not flag_subject:
                    self.statusbar.showMessage("Key not assigned ({})".format(ek_unichr), 5000)


    def twEvents_doubleClicked(self):
        """
        seek video to double clicked position ( add self.repositioningTimeOffset value)
        substract time offset if any
        """

        if self.twEvents.selectedIndexes():

            row = self.twEvents.selectedIndexes()[0].row()

            if ':' in self.twEvents.item(row, 0).text():
                time_ = time2seconds(  self.twEvents.item(row, 0).text()  )
            else:
                time_  = Decimal( self.twEvents.item(row, 0).text() )

            # substract time offset
            time_ -= self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET]

            if time_ + self.repositioningTimeOffset >= 0:
                newTime = (time_ + self.repositioningTimeOffset ) * 1000
            else:
                newTime = 0


            if self.playMode == VLC:

                if len(self.duration) == 1:

                    self.mediaplayer.set_time(int(newTime))

                    if self.simultaneousMedia:
                        # synchronize 2nd player
                        self.mediaplayer2.set_time(int(self.mediaplayer.get_time() - self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] * 1000))

                else: # more media in player 1

                    # remember if player paused (go previous will start playing)
                    flagPaused = self.mediaListPlayer.get_state() == vlc.State.Paused

                    tot = 0
                    for idx, d in enumerate(self.duration):
                        if newTime >= tot and newTime < tot+d:
                            self.mediaListPlayer.play_item_at_index(idx)

                            # wait until media is played
                            while True:
                                if self.mediaListPlayer.get_state() in [vlc.State.Playing, vlc.State.Ended]:
                                    break

                            if flagPaused:
                                self.mediaListPlayer.pause()

                            self.mediaplayer.set_time( newTime -  sum(self.duration[0 : self.media_list.index_of_item(self.mediaplayer.get_media()) ]))
                            break

                        tot += d

                self.timer_out()
                self.timer_spectro_out()


            if self.playMode == FFMPEG:

                frameDuration = Decimal(1000 / list(self.fps.values())[0])

                currentFrame = round( newTime/ frameDuration )

                self.FFmpegGlobalFrame = currentFrame

                if self.FFmpegGlobalFrame > 0:
                    self.FFmpegGlobalFrame -= 1

                self.ffmpegTimerOut()



    def twSubjects_doubleClicked(self):
        '''
        select subject by double-click
        '''

        if self.observationId:
            if self.twSubjects.selectedIndexes():

                row = self.twSubjects.selectedIndexes()[0].row()

                # select or deselect current subject
                if self.currentSubject == self.twSubjects.item(row, 1).text():
                    self.deselectSubject()
                else:
                    self.selectSubject(self.twSubjects.item(row, 1).text())
        else:
            self.no_observation()


    def select_events_between_activated(self):
        '''
        select events between a time interval
        '''

        def parseTime(txt):
            '''
            parse time in string (should be 00:00:00.000 or in seconds)
            '''
            if ':' in txt:
                qtime = QTime.fromString(txt, "hh:mm:ss.zzz")    #timeRegExp(from_)

                if qtime.toString():
                    timeSeconds = time2seconds(qtime.toString("hh:mm:ss.zzz"))
                else:
                    return None
            else:
                try:
                    timeSeconds = Decimal(txt)
                except InvalidOperation:
                    return None
            return timeSeconds


        if self.twEvents.rowCount():
            text, ok = QInputDialog.getText(self, "Select events in time interval", "Interval: (example: 12.5-14.7 or 02:45.780-03:15.120 )", QLineEdit.Normal, "")

            if ok and text != '':

                if not "-" in text:
                    QMessageBox.critical(self, programName, "Use minus sign (-) to separate initial value from final value")
                    return

                while " " in text:
                    text = text.replace(" ", "")

                from_, to_ = text.split("-")[0:2]
                from_sec = parseTime(from_)
                if not from_sec:
                    QMessageBox.critical(self, programName, "Time value not recognized: {}".format(from_))
                    return
                to_sec = parseTime(to_)
                if not to_sec:
                    QMessageBox.critical(self, programName, "Time value not recognized: {}".format(to_))
                    return
                if to_sec < from_sec:
                    QMessageBox.critical(self, programName, "The initial time is greater than the final time")
                    return
                self.twEvents.clearSelection()
                self.twEvents.setSelectionMode( QAbstractItemView.MultiSelection )
                for r in range(0, self.twEvents.rowCount()):
                    if ':' in self.twEvents.item(r, 0).text():
                        time = time2seconds(self.twEvents.item(r, 0).text())
                    else:
                        time = Decimal(self.twEvents.item(r, 0).text())
                    if from_sec <= time <= to_sec:
                        self.twEvents.selectRow(r)

        else:
            QMessageBox.warning(self, programName, "There are no events to select")

    def delete_all_events(self):
        """
        delete all events in current observation
        """

        if not self.observationId:
            self.no_observation()
            return

        if not self.pj[OBSERVATIONS][self.observationId][EVENTS]:
            QMessageBox.warning(self, programName, "No events to delete")
            return

        if dialog.MessageDialog(programName, "Do you really want to delete all events from the current observation?", [YES, NO]) == YES:
            self.pj[OBSERVATIONS][self.observationId][EVENTS] = []
            self.projectChanged = True
            self.loadEventsInTW(self.observationId)


    def delete_selected_events(self):
        '''
        delete selected observations
        '''

        if not self.observationId:
            self.no_observation()
            return

        if not self.twEvents.selectedIndexes():
            QMessageBox.warning(self, programName, "No event selected!")
        else:
            # list of rows to delete (set for unique)
            rows = set([item.row() for item in self.twEvents.selectedIndexes()])
            self.pj[OBSERVATIONS][self.observationId][EVENTS] = [event for idx, event in enumerate(self.pj[OBSERVATIONS][self.observationId][EVENTS]) if not idx in rows]
            self.projectChanged = True
            self.loadEventsInTW( self.observationId )


    def edit_selected_events(self):
        """
        edit one or more selected events for subject, behavior and/or comment
        """
        # list of rows to edit
        rowsToEdit = set([item.row() for item in self.twEvents.selectedIndexes()])

        if not len(rowsToEdit):
            QMessageBox.warning(self, programName, "No event selected!")
        elif len(rowsToEdit) == 1:  # 1 event selected
            self.edit_event()
        else:  # editing of more events
            dialogWindow = dialog.EditSelectedEvents()
            dialogWindow.all_behaviors = [self.pj[ETHOGRAM][str(k)]["code"] for k in sorted([int(x) for x in self.pj[ETHOGRAM].keys()])]
            dialogWindow.all_subjects = [self.pj[SUBJECTS][str(k)]["name"] for k in sorted([int(x) for x in self.pj[SUBJECTS].keys()])]

            if dialogWindow.exec_():
                for idx, event in enumerate(self.pj[OBSERVATIONS][self.observationId][EVENTS]):
                    if idx in rowsToEdit:
                        if dialogWindow.rbSubject.isChecked():
                            event[EVENT_SUBJECT_FIELD_IDX] = dialogWindow.newText.selectedItems()[0].text()
                        if dialogWindow.rbBehavior.isChecked():
                            event[EVENT_BEHAVIOR_FIELD_IDX] = dialogWindow.newText.selectedItems()[0].text()
                        if dialogWindow.rbComment.isChecked():
                            event[EVENT_COMMENT_FIELD_IDX] = dialogWindow.commentText.text()

                        self.pj[OBSERVATIONS][self.observationId][EVENTS][idx] = event
                        self.projectChanged = True
                self.loadEventsInTW(self.observationId)


    def click_signal_find_in_events(self, msg):
        """
        find in events when "Find" button of find dialog box is pressed
        """

        if msg == "CLOSE":
            self.find_dialog.close()
            return
        if not self.find_dialog.findText.text():
            #QMessageBox.warning(self, programName, "Nothing to find", QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)
            return

        fields_list = []
        if self.find_dialog.cbSubject.isChecked():
            fields_list.append(EVENT_SUBJECT_FIELD_IDX)
        if self.find_dialog.cbBehavior.isChecked():
            fields_list.append(EVENT_BEHAVIOR_FIELD_IDX)
        if self.find_dialog.cbModifier.isChecked():
            fields_list.append(EVENT_MODIFIER_FIELD_IDX)
        if self.find_dialog.cbComment.isChecked():
            fields_list.append(EVENT_COMMENT_FIELD_IDX)

        for event_idx, event in enumerate(self.pj[OBSERVATIONS][self.observationId][EVENTS]):
            if event_idx <= self.find_dialog.currentIdx:
                continue
            if (not self.find_dialog.cbFindInSelectedEvents.isChecked()) or (self.find_dialog.cbFindInSelectedEvents.isChecked() and event_idx in self.find_dialog.rowsToFind):
                for idx in fields_list:
                    if self.find_dialog.findText.text() in event[idx]:
                        self.find_dialog.currentIdx = event_idx
                        self.twEvents.scrollToItem(self.twEvents.item(event_idx, 0))
                        self.twEvents.selectRow(event_idx)
                        return

        if dialog.MessageDialog(programName, "<b>{}</b> not found! Search from beginning?".format(self.find_dialog.findText.text()), [YES, NO]) == YES:
            self.find_dialog.currentIdx = -1
            self.click_signal_find_in_events("FIND")
        else:
            self.find_dialog.close()



    def find_events(self):
        """
        find  in events
        """

        self.find_dialog = dialog.FindInEvents()
        # list of rows to find
        self.find_dialog.rowsToFind = set([item.row() for item in self.twEvents.selectedIndexes()])
        self.find_dialog.currentIdx = -1
        self.find_dialog.clickSignal.connect(self.click_signal_find_in_events)
        self.find_dialog.setWindowFlags(Qt.WindowStaysOnTopHint)
        self.find_dialog.show()


    def click_signal_find_replace_in_events(self, msg):
        """
        find/replace in events when "Find" button of find dialog box is pressed
        """

        if msg == "CANCEL":
            self.find_replace_dialog.close()
            return
        if not self.find_replace_dialog.findText.text():
            dialog.MessageDialog(programName, "There is nothing to find.", ["OK"])
            return

        if self.find_replace_dialog.cbFindInSelectedEvents.isChecked() and not len(self.find_replace_dialog.rowsToFind):
            dialog.MessageDialog(programName, "There are no selected events", ["OK"])
            return

        fields_list = []
        if self.find_replace_dialog.cbSubject.isChecked():
            fields_list.append(EVENT_SUBJECT_FIELD_IDX)
        if self.find_replace_dialog.cbBehavior.isChecked():
            fields_list.append(EVENT_BEHAVIOR_FIELD_IDX)
        if self.find_replace_dialog.cbModifier.isChecked():
            fields_list.append(EVENT_MODIFIER_FIELD_IDX)
        if self.find_replace_dialog.cbComment.isChecked():
            fields_list.append(EVENT_COMMENT_FIELD_IDX)

        number_replacement = 0
        for event_idx, event in enumerate(self.pj[OBSERVATIONS][self.observationId][EVENTS]):

            if event_idx < self.find_replace_dialog.currentIdx:
                continue

            if (not self.find_replace_dialog.cbFindInSelectedEvents.isChecked()) or (self.find_replace_dialog.cbFindInSelectedEvents.isChecked() and event_idx in self.find_replace_dialog.rowsToFind):
                for idx1 in fields_list:
                    if idx1 <= self.find_replace_dialog.currentIdx_idx:
                        continue
                    if self.find_replace_dialog.findText.text() in event[idx1]:
                        number_replacement += 1
                        self.find_replace_dialog.currentIdx = event_idx
                        self.find_replace_dialog.currentIdx_idx = idx1
                        event[idx1] = event[idx1].replace(self.find_replace_dialog.findText.text(), self.find_replace_dialog.replaceText.text())
                        self.pj[OBSERVATIONS][self.observationId][EVENTS][event_idx] = event
                        self.loadEventsInTW(self.observationId)
                        self.twEvents.scrollToItem(self.twEvents.item(event_idx, 0))
                        self.twEvents.selectRow(event_idx)
                        self.projectChanged = True

                        if msg == "FIND_REPLACE":
                            return

                self.find_replace_dialog.currentIdx_idx = -1

        if msg == "FIND_REPLACE":
            if dialog.MessageDialog(programName, "{} not found.\nRestart find/replace from the beginning?".format(self.find_replace_dialog.findText.text()), [YES, NO]) == YES:
                self.find_replace_dialog.currentIdx = -1
            else:
                self.find_replace_dialog.close()
        if msg == "FIND_REPLACE_ALL":
            dialog.MessageDialog(programName, "{} substitution(s).".format(number_replacement), [OK])
            self.find_replace_dialog.close()


    def find_replace_events(self):
        """
        find and replace in events
        """
        self.find_replace_dialog = dialog.FindReplaceEvents()
        self.find_replace_dialog.currentIdx = -1
        self.find_replace_dialog.currentIdx_idx = -1
        # list of rows to find/replace
        self.find_replace_dialog.rowsToFind = set([item.row() for item in self.twEvents.selectedIndexes()])
        self.find_replace_dialog.clickSignal.connect(self.click_signal_find_replace_in_events)
        self.find_replace_dialog.setWindowFlags(Qt.WindowStaysOnTopHint)
        self.find_replace_dialog.show()



    def export_tabular_events(self):
        """
        export events from selected observations in various formats: TSV, CSV, ODS, XLS
        """

        # ask user observations to analyze
        result, selectedObservations = self.selectObservations(MULTIPLE)

        if not selectedObservations:
            return

        plot_parameters = self.choose_obs_subj_behav_category(selectedObservations, maxTime=0, flagShowIncludeModifiers=False, flagShowExcludeBehaviorsWoEvents=False)

        if not plot_parameters["selected subjects"] or not plot_parameters["selected behaviors"]:
            return

        includeMediaInfo = None
        for obsId in selectedObservations:
            if self.pj[OBSERVATIONS][obsId]["type"] in [MEDIA]:
                includeMediaInfo = YES
                break

        if len(selectedObservations) > 1:  # choose directory for exporting more observations

            items = ("Tab Separated Values (*.tsv)", "Comma separated values (*.csv)", "Open Document Spreadsheet (*.ods)", "Microsoft Excel (*.xls)", "HTML (*.html)")
            item, ok = QInputDialog.getItem(self, "Export events format", "Available formats", items, 0, False)
            if not ok:
                return
            outputFormat = re.sub(".* \(\*\.", "", item)[:-1]

            exportDir = QFileDialog(self).getExistingDirectory(self, "Choose a directory to export events", os.path.expanduser("~"), options=QFileDialog.ShowDirsOnly)
            if not exportDir:
                return

        for obsId in selectedObservations:
            if len(selectedObservations) == 1:
                fileFormats = ("Tab Separated Values (*.txt *.tsv);;"
                               "Comma Separated Values (*.txt *.csv);;"
                               "Microsoft Excel XLS (*.xls);;"
                               "Open Document Spreadsheet ODS (*.ods);;"
                               "HTML (*.html);;"
                               "All files (*)")
                while True:
                    if QT_VERSION_STR[0] == "4":
                        fileName, filter_ = QFileDialog(self).getSaveFileNameAndFilter(self, "Export events", "", fileFormats)
                    else:
                        fileName, filter_ = QFileDialog(self).getSaveFileName(self, "Export events", "", fileFormats)

                    if not fileName:
                        return

                    outputFormat = ""
                    availableFormats = ("tsv", "csv", "xls", "ods", "html")
                    for fileExtension in availableFormats:
                        if fileExtension in filter_:
                            outputFormat = fileExtension
                            if not fileName.upper().endswith("." + fileExtension.upper()):
                                fileName += "." + fileExtension

                    if not outputFormat:
                        QMessageBox.warning(self, programName, "Choose a file format", QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)
                    else:
                        break

            else:

                fileName = exportDir + os.sep + safeFileName(obsId) + "." + outputFormat

            eventsWithStatus = self.update_events_start_stop2(self.pj[OBSERVATIONS][obsId][EVENTS])

            # check max number of modifiers
            max_modifiers = 0
            for event in eventsWithStatus:
                for c in pj_events_fields:
                    if c == "modifier" and event[pj_obs_fields[c]]:
                        max_modifiers = max(max_modifiers, len(event[pj_obs_fields[c]].split('|')))

            # media file number
            mediaNb = 0
            if self.pj[OBSERVATIONS][obsId]["type"] in [MEDIA]:
                for idx in self.pj[OBSERVATIONS][obsId][FILE]:
                    for media in self.pj[OBSERVATIONS][obsId][FILE][idx]:
                        mediaNb += 1

            rows = []

            # observation id
            rows.append(["Observation id", obsId])
            rows.append([""])

            # media file name
            if self.pj[OBSERVATIONS][obsId]["type"] in [MEDIA]:
                rows.append(["Media file(s)"])
            else:
                rows.append(["Live observation"])
            rows.append([""])

            if self.pj[OBSERVATIONS][obsId][TYPE] in [MEDIA]:

                for idx in self.pj[OBSERVATIONS][obsId][FILE]:
                    for media in self.pj[OBSERVATIONS][obsId][FILE][idx]:
                        rows.append(["Player #{0}".format(idx), media])
            rows.append([""])

            # date
            if "date" in self.pj[OBSERVATIONS][obsId]:
                rows.append(["Observation date", self.pj[OBSERVATIONS][obsId]["date"].replace("T", " ")])
            rows.append([""])

            # description
            if "description" in self.pj[OBSERVATIONS][obsId]:
                rows.append(["Description", eol2space(self.pj[OBSERVATIONS][obsId]["description"])])
            rows.append([""])

            # time offset
            if "time offset" in self.pj[OBSERVATIONS][obsId]:
                rows.append(["Time offset (s)", self.pj[OBSERVATIONS][obsId]["time offset"]])
            rows.append([""])

            # independent variables
            if "independent_variables" in self.pj[OBSERVATIONS][obsId]:
                rows.append(["independent variables"])

                rows.append(["variable", "value"])

                for variable in self.pj[OBSERVATIONS][obsId]["independent_variables"]:
                    rows.append([variable, self.pj[OBSERVATIONS][obsId]["independent_variables"][variable]])

            rows.append([""])

            # write table header
            col = 0
            header = ["Time"]
            if includeMediaInfo == YES:
                header.extend(["Media file path", "Media total length", "FPS"])

            header.extend(["Subject", "Behavior"])
            for x in range(1, max_modifiers + 1):
                header.append("Modifier {}".format(x))
            header.extend(["Comment", "Status"])

            rows.append(header)

            duration1 = []   # in seconds
            if self.pj[OBSERVATIONS][obsId]["type"] in [MEDIA]:
                try:
                    for mediaFile in self.pj[OBSERVATIONS][obsId][FILE][PLAYER1]:
                        duration1.append(self.pj[OBSERVATIONS][obsId]["media_info"]["length"][mediaFile])
                except:
                    pass

            for event in eventsWithStatus:

                if (((event[SUBJECT_EVENT_FIELD] in plot_parameters["selected subjects"])
                   or (event[SUBJECT_EVENT_FIELD] == "" and NO_FOCAL_SUBJECT in plot_parameters["selected subjects"]))
                   and (event[BEHAVIOR_EVENT_FIELD] in plot_parameters["selected behaviors"])):

                    fields = []
                    fields.append(intfloatstr(str(event[EVENT_TIME_FIELD_IDX])))

                    if includeMediaInfo == YES:

                        time_ = event[EVENT_TIME_FIELD_IDX] - self.pj[OBSERVATIONS][obsId][TIME_OFFSET]
                        if time_ < 0:
                            time_ = 0

                        mediaFileIdx = [idx1 for idx1, x in enumerate(duration1) if time_ >= sum(duration1[0:idx1])][-1]
                        fields.append(intfloatstr(str(self.pj[OBSERVATIONS][obsId][FILE][PLAYER1][mediaFileIdx])))
                        # media total length
                        fields.append(str(sum([float(x) for x in duration1])))
                        # fps
                        fields.append(self.pj[OBSERVATIONS][obsId]["media_info"]["fps"][self.pj[OBSERVATIONS][obsId][FILE][PLAYER1][mediaFileIdx]])

                    fields.append(event[EVENT_SUBJECT_FIELD_IDX])
                    fields.append(event[EVENT_BEHAVIOR_FIELD_IDX])

                    modifiers = event[EVENT_MODIFIER_FIELD_IDX].split("|")
                    while len(modifiers) < max_modifiers:
                        modifiers.append("")

                    for m in modifiers:
                        fields.append(m)
                    fields.append(event[EVENT_COMMENT_FIELD_IDX].replace(os.linesep, " "))
                    # status
                    fields.append(event[-1])

                    rows.append(fields)

            maxLen = max([len(r) for r in rows])
            data = tablib.Dataset()

            # check if worksheet name will be > 31 char
            if outputFormat == "xls":
                if len(obsId) > 31:
                    data.title = obsId[0:31]
                    QMessageBox.warning(None, programName, ("The worksheet name <b>{0}</b> was shortened to <b>{1}</b> due to XLS format limitations.\n"
                                                            "The limit on worksheet name length is 31 characters").format(obsId, obsId[0:31]),
                                        QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)

            else:
                data.title = obsId

            for row in rows:
                data.append( complete(row, maxLen))

            try:
                if outputFormat == "tsv":
                    with open(fileName, "wb") as f:
                        f.write(str.encode(data.tsv))
                if outputFormat == "csv":
                    with open(fileName, "wb") as f:
                        f.write(str.encode(data.csv))
                if outputFormat == "ods":
                    with open(fileName, "wb") as f:
                        f.write(data.ods)
                if outputFormat == "xls":
                    with open(fileName, "wb") as f:
                        f.write(data.xls)
                if outputFormat == "html":
                    with open(fileName, "wb") as f:
                        f.write(str.encode(data.html))

                '''
                if outputFormat == "xlsx":
                    with open(fileName, "wb") as f:
                        f.write(data.xlsx)
                '''

            except:
                #errorMsg = sys.exc_info()[1].strerror
                errorMsg = sys.exc_info()[1]

                logging.critical(errorMsg)
                QMessageBox.critical(None, programName, str(errorMsg), QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)

            del data

        self.statusbar.showMessage("Events exported", 0)


    def create_behavioral_strings(self, obsId, subj, plot_parameters):
        """
        return the behavioral string for subject in obsId
        """

        s = ""
        currentStates = []
        eventsWithStatus = self.update_events_start_stop2(self.pj[OBSERVATIONS][obsId][EVENTS])

        for event in eventsWithStatus:
            if event[EVENT_SUBJECT_FIELD_IDX] == subj or (subj == NO_FOCAL_SUBJECT and event[EVENT_SUBJECT_FIELD_IDX] == ""):

                if event[-1] == POINT:
                    if currentStates:
                        #s += "+".join(replace_spaces(currentStates)) + "+" + event[EVENT_BEHAVIOR_FIELD_IDX]   #.replace(" ", "_")
                        s += "+".join(currentStates) + "+" + event[EVENT_BEHAVIOR_FIELD_IDX]
                    else:
                        s += event[EVENT_BEHAVIOR_FIELD_IDX]    #.replace(" ", "_")

                    if plot_parameters["include modifiers"]:
                        s += "&" + event[EVENT_MODIFIER_FIELD_IDX].replace("|", "+")

                    s += self.behaviouralStringsSeparator

                if event[-1] == START:
                    currentStates.append(event[EVENT_BEHAVIOR_FIELD_IDX])
                    #s += "+".join(replace_spaces(currentStates))
                    s += "+".join(currentStates)

                    if plot_parameters["include modifiers"]:
                        s += "&" + event[EVENT_MODIFIER_FIELD_IDX].replace("|", "+")
                    s += self.behaviouralStringsSeparator

                if event[-1] == STOP:

                    if event[EVENT_BEHAVIOR_FIELD_IDX] in currentStates:
                        currentStates.remove(event[EVENT_BEHAVIOR_FIELD_IDX])

                    if currentStates:
                        #s += "+".join(replace_spaces(currentStates))
                        s += "+".join(currentStates)

                        if plot_parameters["include modifiers"]:
                            s += "&" + event[EVENT_MODIFIER_FIELD_IDX].replace("|", "+")
                        s += self.behaviouralStringsSeparator

        # remove last separator (if separator not empty)
        if self.behaviouralStringsSeparator:
            s = s[0: -len(self.behaviouralStringsSeparator)]

        return s


    def export_string_events(self):
        """
        export events from selected observations by subject as behavioral strings (plain text file)
        behaviors are separated by character specified in self.behaviouralStringsSeparator (usually pipe |)
        for use with BSA (see http://penelope.unito.it/bsa)
        """

        # ask user observations to analyze
        result, selectedObservations = self.selectObservations(MULTIPLE)
        if not selectedObservations:
            return

        plot_parameters = self.choose_obs_subj_behav_category(selectedObservations, maxTime=0, flagShowIncludeModifiers=True, flagShowExcludeBehaviorsWoEvents=False)

        if not plot_parameters["selected subjects"] or not plot_parameters["selected behaviors"]:
            return

        if QT_VERSION_STR[0] == "4":
            fileName = QFileDialog(self).getSaveFileName(self, "Export events as strings", "", "Events file (*.txt *.tsv);;All files (*)")
        else:
            fileName, _ = QFileDialog(self).getSaveFileName(self, "Export events as strings", "", "Events file (*.txt *.tsv);;All files (*)")

        if fileName:

            response = dialog.MessageDialog(programName, "Include observation(s) information?", [YES, NO])

            try:
                with open(fileName, "w", encoding="utf-8") as outFile:
                    for obsId in selectedObservations:
                        # observation id
                        outFile.write("\n# observation id: {0}\n".format(obsId))
                        # observation descrition
                        outFile.write("# observation description: {0}\n".format(self.pj[OBSERVATIONS][obsId]["description"].replace(os.linesep, " ")))
                        # media file name
                        if self.pj[OBSERVATIONS][obsId][TYPE] in [MEDIA]:
                            outFile.write("# Media file name: {0}{1}{1}".format(", ".join([os.path.basename(x) for x in self.pj[OBSERVATIONS][obsId][FILE][PLAYER1]]), os.linesep))
                        if self.pj[OBSERVATIONS][obsId][TYPE] in [LIVE]:
                            outFile.write("# Live observation{0}{0}".format(os.linesep))

                        # independent variables
                        if "independent_variables" in self.pj[OBSERVATIONS][obsId]:
                            outFile.write("# Independent variables\n")

                            # rows.append(["variable", "value"])
                            for variable in self.pj[OBSERVATIONS][obsId]["independent_variables"]:
                                outFile.write("# {0}: {1}\n".format(variable, self.pj[OBSERVATIONS][obsId]["independent_variables"][variable]))
                        outFile.write("\n")

                        # selected subjects
                        for subj in plot_parameters["selected subjects"]:
                            if subj:
                                subj_str = "\n# {}:\n".format(subj)
                            else:
                                subj_str = "\n# No focal subject:\n"
                            outFile.write(subj_str)

                            out = self.create_behavioral_strings(obsId, subj, plot_parameters)
                            if out:
                                outFile.write(out + "\n")

            except:
                errorMsg = sys.exc_info()[1]
                logging.critical(errorMsg)
                QMessageBox.critical(None, programName, str(errorMsg), QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)



    def transitions_matrix(self, mode):
        """
        create transitions frequencies matrix with selected observations, subjects and behaviors
        mode:
        * frequency
        * number
        * frequencies_after_behaviors
        """
        # ask user observations to analyze
        result, selectedObservations = self.selectObservations(MULTIPLE)
        if not selectedObservations:
            return

        plot_parameters = self.choose_obs_subj_behav_category(selectedObservations, maxTime=0, flagShowIncludeModifiers=True, flagShowExcludeBehaviorsWoEvents=False)

        if not plot_parameters["selected subjects"] or not plot_parameters["selected behaviors"]:
            return

        flagMulti = False
        if len(plot_parameters["selected subjects"]) == 1:
            if QT_VERSION_STR[0] == "4":
                fileName = QFileDialog(self).getSaveFileName(self, "Create matrix of transitions " + mode, "", "Transitions matrix files (*.txt *.tsv);;All files (*)")
            else:
                fileName, _ = QFileDialog(self).getSaveFileName(self, "Create matrix of transitions " + mode, "", "Transitions matrix files (*.txt *.tsv);;All files (*)")
        else:
            exportDir = QFileDialog(self).getExistingDirectory(self, "Choose a directory to save the transitions matrices", os.path.expanduser("~"), options=QFileDialog(self).ShowDirsOnly)
            if not exportDir:
                return
            flagMulti = True

        for subject in plot_parameters["selected subjects"]:

            logging.debug("subjects: {}".format(subject))

            strings_list = []
            for obsId in selectedObservations:
                strings_list.append(self.create_behavioral_strings(obsId, subject, plot_parameters))


            print("strings_list", strings_list)
            sequences, observed_behaviors = transitions.behavioral_strings_analysis(strings_list, self.behaviouralStringsSeparator)

            print("observed behaviors", observed_behaviors)

            observed_matrix = transitions.observed_transitions_matrix(sequences, sorted(list(set(observed_behaviors + plot_parameters["selected behaviors"]))), mode=mode)

            if not observed_matrix:
                QMessageBox.warning(self, programName, "No transitions found for <b>{}</b>".format(subject))
                continue

            logging.debug("observed_matrix {}:\n{}".format(mode, observed_matrix))

            if flagMulti:
                try:

                    nf = "{exportDir}{sep}{subject}_transitions_{mode}_matrix.tsv".format(exportDir=exportDir,
                                                                                          sep=os.sep,
                                                                                          subject=subject,
                                                                                          mode=mode)

                    if os.path.isfile(nf):
                        if dialog.MessageDialog(programName, "A file with same name already exists.<br><b>{}</b>".format(nf), ["Overwrite", CANCEL]) == CANCEL:
                            continue

                    with open(nf, "w") as outfile:
                        outfile.write(observed_matrix)
                except:
                    QMessageBox.critical(self, programName, "The file {} can not be saved".format(nf))
            else:
                try:
                    with open(fileName, "w") as outfile:
                        outfile.write(observed_matrix)

                except:
                    QMessageBox.critical(self, programName, "The file {} can not be saved".format(fileName))


    def transitions_dot_script(self):
        """
        create dot script (graphviz language) from transitions frequencies matrix
        """
        if QT_VERSION_STR[0] == "4":
            fileNames = QFileDialog(self).getOpenFileNames(self, "Select one or more transitions matrix files", "", "Transitions matrix files (*.txt *.tsv);;All files (*)")
        else:
            fileNames, _ = QFileDialog(self).getOpenFileNames(self, "Select one or more transitions matrix files", "", "Transitions matrix files (*.txt *.tsv);;All files (*)")

        out = ""
        for fileName in fileNames:
            with open(fileName, "r") as infile:
                gv = transitions.create_transitions_gv_from_matrix(infile.read(), cutoff_all=0, cutoff_behavior=0, edge_label="percent_node")

                print(gv, file=open(fileName + ".gv", "w"))

                out += "<b>{}</b> created<br>".format(fileName + ".gv")

                '''
                not working with PyQt 5.7 on Windows
                gv_svg = transitions.create_diagram_from_gv(gv)
                try:
                    print(gv_svg, file=open(fileName + ".svg", "w"))
                    out += "{} created\n".format(fileName + ".svg")
                except:
                    QMessageBox.critical(self, programName, "The file {} can not be saved".format(fileName + ".svg"))
                '''

        if out:
            QMessageBox.information(self, programName, out + "<br><br>The DOT scripts can be used with Graphviz or WebGraphviz to generate diagram")


    def transitions_flow_diagram(self):
        """
        create flow diagram with graphviz (if installed) from transitions matrix
        """

        # check if dot present in path
        result = subprocess.getoutput("dot -V")
        if "graphviz" not in result:
            QMessageBox.critical(self, programName, ("The GraphViz package is not installed.<br>"
                                                     "The <b>dot</b> program was not found in the path.<br><br>"
                                                     """Go to <a href="http://www.graphviz.org">http://www.graphviz.org</a> for information"""))
            return

        if QT_VERSION_STR[0] == "4":
            fileNames = QFileDialog(self).getOpenFileNames(self, "Select one or more transitions matrix files", "", "Transitions matrix files (*.txt *.tsv);;All files (*)")
        else:
            fileNames, _ = QFileDialog(self).getOpenFileNames(self, "Select one or more transitions matrix files", "", "Transitions matrix files (*.txt *.tsv);;All files (*)")

        out = ""
        for fileName in fileNames:
            with open(fileName, "r") as infile:
                gv = transitions.create_transitions_gv_from_matrix(infile.read(), cutoff_all=0, cutoff_behavior=0, edge_label="percent_node")

                print(gv, file=open(tempfile.gettempdir() + os.sep + os.path.basename(fileName) + ".tmp.gv", "w"))

                #result = subprocess.getoutput("""echo '{0}' | dot -Tpng -o "{1}.png" """.format(gv.replace("\n", ""), fileName))
                result = subprocess.getoutput("""dot -Tpng -o "{0}.png" "{1}" """.format(fileName, tempfile.gettempdir() + os.sep + os.path.basename(fileName) + ".tmp.gv"))

                if not result:
                    out += "<b>{}</b> created<br>".format(fileName + ".png")
                else:
                    out += "Problem with <b>{}</b><br>".format(fileName)


        if out:
            QMessageBox.information(self, programName, out)


    def closeEvent(self, event):
        """
        check if current project is saved
        close coding pad window if it exists
        close spectrogram window if it exists
         and close program
        """

        # check if re-encoding
        if self.ffmpeg_recode_process:
            QMessageBox.warning(self, programName, "BORIS is re-encoding/resizing a video. Please wait before closing.")
            event.ignore()

        if self.projectChanged:
            response = dialog.MessageDialog(programName, "What to do about the current unsaved project?", [SAVE, DISCARD, CANCEL])

            if response == SAVE:
                if self.save_project_activated() == "not saved":
                    event.ignore()

            if response == CANCEL:
                event.ignore()

        self.saveConfigFile()

        self.close_tool_windows()


    def actionQuit_activated(self):
        self.close()



    def import_observations(self):
        """
        import observations from project file
        """

        if QT_VERSION_STR[0] == "4":
            fileName = QFileDialog(self).getOpenFileName(self, "Choose a BORIS project file", "", "Project files (*.boris);;Old project files (*.obs);;All files (*)")
        else:
            fileName, _ = QFileDialog(self).getOpenFileName(self, "Choose a BORIS project file", "", "Project files (*.boris);;Old project files (*.obs);;All files (*)")

        if self.projectFileName and fileName == self.projectFileName:
            QMessageBox.critical(None, programName, "This project is already open", QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)
            return

        if fileName:

            try:
                fromProject = json.loads(open(fileName, "r").read())
            except:
                QMessageBox.critical(self, programName, "This project file seems corrupted")
                return

            # transform time to decimal
            fromProject = self.convert_time_to_decimal(fromProject)

            dbc = dialog.ChooseObservationsToImport("Choose the observations to import:", sorted(list(fromProject[OBSERVATIONS].keys())))

            if dbc.exec_():

                selected_observations = dbc.get_selected_observations()
                if selected_observations:
                    flagImported = False

                    # set of behaviors in current projet ethogram
                    behav_set = set([self.pj[ETHOGRAM][idx]["code"] for idx in self.pj[ETHOGRAM]])

                    # set of subjects in current projet
                    subjects_set = set([self.pj[SUBJECTS][idx]["name"] for idx in self.pj[SUBJECTS]])

                    for obsId in selected_observations:

                        # check if behaviors are in current project ethogram
                        new_behav_set = set([event[EVENT_BEHAVIOR_FIELD_IDX] for event in fromProject[OBSERVATIONS][obsId][EVENTS] if event[EVENT_BEHAVIOR_FIELD_IDX] not in behav_set])
                        if new_behav_set:
                            if dialog.MessageDialog(programName, "Some coded behaviors in <b>{}</b> are not in the ethogram:<br><b>{}</b>".format(obsId, ", ".join(new_behav_set)), ["Skip observation", "Import observation"]) == "Skip observation":
                                continue

                        # check if subjects are in current project
                        new_subject_set = set([event[EVENT_SUBJECT_FIELD_IDX] for event in fromProject[OBSERVATIONS][obsId][EVENTS] if event[EVENT_SUBJECT_FIELD_IDX] not in subjects_set])
                        if new_subject_set and new_subject_set != {''}:
                            if dialog.MessageDialog(programName, "Some coded subjects in <b>{}</b> are not defined in the project:<br><b>{}</b>".format(obsId, ", ".join(new_subject_set)), ["Skip observation", "Import observation"]) == "Skip observation":
                                continue

                        if obsId in self.pj[OBSERVATIONS].keys():
                            if dialog.MessageDialog(programName, "The observation <b>{}</b> already exists in the current project.<br>".format(obsId), ["Skip observation", "Rename observation"]) == "Rename observation":
                                self.pj[OBSERVATIONS]["{} (imported at {})".format(obsId, datetime_iso8601())] = dict(fromProject[OBSERVATIONS][obsId])
                                flagImported = True
                        else:
                            self.pj[OBSERVATIONS][obsId] = dict(fromProject[OBSERVATIONS][obsId])
                            flagImported = True

                    if flagImported:
                        QMessageBox.information(self, programName, "Observations imported successfully")


    def play_video(self):
        """
        play video
        """

        if self.playerType == VLC:
            if self.playMode == FFMPEG:
                self.FFmpegTimer.start()
            else:
                self.mediaListPlayer.play()

                # second video together
                if self.simultaneousMedia:
                    self.mediaListPlayer2.play()

                self.timer.start(200)
                self.timer_spectro.start()


    def pause_video(self):
        """
        pause media
        do not pause media if already paused (otherwise media will be played)
        """

        if self.playerType == VLC:

            if self.playMode == FFMPEG:
                self.FFmpegTimer.stop()
            else:

                if self.mediaListPlayer.get_state() != vlc.State.Paused:

                    self.timer.stop()
                    self.timer_spectro.stop()
                    self.mediaListPlayer.pause()
                    # wait for pause

                    # wait until video is paused or ended
                    while True:
                        if self.mediaListPlayer.get_state() in [vlc.State.Paused, vlc.State.Ended]:
                            break

                # second video together
                if self.simultaneousMedia:
                    if self.mediaListPlayer2.get_state() != vlc.State.Paused:
                        self.mediaListPlayer2.pause()

                logging.debug("pause_video: player #1 state: {}".format(self.mediaListPlayer.get_state()))
                if self.simultaneousMedia:
                    logging.debug('pause_video: player #2 state {}'.format(self.mediaListPlayer2.get_state()))
                    pass

                time.sleep(1)
                self.timer_out()
                self.timer_spectro_out()


    def play_activated(self):
        """
        button 'play' activated
        """
        if self.observationId and self.pj[OBSERVATIONS][self.observationId][TYPE] in [MEDIA]:
            self.play_video()


    def jumpBackward_activated(self):
        '''
        rewind from current position
        '''
        if self.playerType == VLC:

            if self.playMode == FFMPEG:
                currentTime = self.FFmpegGlobalFrame / list(self.fps.values())[0]
                if int((currentTime - self.fast) * list(self.fps.values())[0]) > 0:
                    self.FFmpegGlobalFrame = int((currentTime - self.fast) * list(self.fps.values())[0])
                else:
                    self.FFmpegGlobalFrame = 0   # position to init
                if self.second_player():
                    currentTime2 = self.FFmpegGlobalFrame2 / list(self.fps2.values())[0]
                    if int((currentTime2 - self.fast) * list(self.fps2.values())[0]) > 0:
                        self.FFmpegGlobalFrame2 = int((currentTime2 - self.fast) * list(self.fps2.values())[0])
                    else:
                        self.FFmpegGlobalFrame2 = 0   # position to init
                self.ffmpegTimerOut()
            else:
                if self.media_list.count() == 1:
                    if self.mediaplayer.get_time() >= self.fast * 1000:
                        self.mediaplayer.set_time( self.mediaplayer.get_time() - self.fast * 1000)
                    else:
                        self.mediaplayer.set_time(0)
                    if self.simultaneousMedia:
                        self.mediaplayer2.set_time(int(self.mediaplayer.get_time() - self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] * 1000) )



                elif self.media_list.count() > 1:

                    newTime = (sum(self.duration[0 : self.media_list.index_of_item(self.mediaplayer.get_media()) ]) + self.mediaplayer.get_time()) - self.fast * 1000
                    if newTime < self.fast * 1000:
                        newTime = 0

                    logging.debug('newTime: {0}'.format(newTime))
                    logging.debug('sum self.duration: {0}'.format(sum(self.duration)))

                    # remember if player paused (go previous will start playing)
                    flagPaused = self.mediaListPlayer.get_state() == vlc.State.Paused

                    logging.debug('flagPaused: {0}'.format(flagPaused))

                    tot = 0
                    for idx, d in enumerate(self.duration):
                        if newTime >= tot and newTime < tot+d:
                            self.mediaListPlayer.play_item_at_index(idx)

                            # wait until media is played
                            while True:
                                if self.mediaListPlayer.get_state() in [vlc.State.Playing, vlc.State.Ended]:
                                    break

                            if flagPaused:
                                self.mediaListPlayer.pause()

                            self.mediaplayer.set_time( newTime -  sum(self.duration[0 : self.media_list.index_of_item(self.mediaplayer.get_media()) ]))

                            break
                        tot += d

                else:
                    self.no_media()

                self.timer_out()
                self.timer_spectro_out()

                # no subtitles
                #self.mediaplayer.video_set_spu(0)


    def jumpForward_activated(self):
        """
        forward from current position
        """

        if self.playerType == VLC:

            if self.playMode == FFMPEG:
                '''
                currentTime = self.FFmpegGlobalFrame / list(self.fps.values())[0]
                self.FFmpegGlobalFrame = int((currentTime + self.fast) * list(self.fps.values())[0])
                '''

                self.FFmpegGlobalFrame += self.fast * list(self.fps.values())[0]


                if self.FFmpegGlobalFrame * (1000 / list(self.fps.values())[0]) >= sum(self.duration):
                    logging.debug("end of last media")
                    self.FFmpegGlobalFrame = int(sum(self.duration) * list(self.fps.values())[0] / 1000)-1
                    logging.debug("FFmpegGlobalFrame {}  sum duration {}".format(self.FFmpegGlobalFrame, sum(self.duration)))

                if self.FFmpegGlobalFrame > 0:
                    self.FFmpegGlobalFrame -= 1

                if self.second_player():
                    '''
                    currentTime2 = self.FFmpegGlobalFrame2 / list(self.fps2.values())[0]
                    self.FFmpegGlobalFrame2 = int((currentTime2 + self.fast) * list(self.fps2.values())[0])
                    '''

                    self.FFmpegGlobalFrame2 += self.fast * list(self.fps2.values())[0]
                    if self.FFmpegGlobalFrame2 * (1000 / list(self.fps2.values())[0]) >= sum(self.duration2):
                        logging.debug("end of last media")
                        self.FFmpegGlobalFrame2 = int(sum(self.duration2) * list(self.fps2.values())[0] / 1000)-1
                        logging.debug("FFmpegGlobalFrame2 {}  sum duration2 {}".format(self.FFmpegGlobalFrame2, sum(self.duration2)))


                    if self.FFmpegGlobalFrame2 > 0:
                        self.FFmpegGlobalFrame2 -= 1

                self.ffmpegTimerOut()

            else:
                if self.media_list.count() == 1:
                    if self.mediaplayer.get_time() >= self.mediaplayer.get_length() - self.fast * 1000:
                        self.mediaplayer.set_time(self.mediaplayer.get_length())
                    else:
                        self.mediaplayer.set_time(self.mediaplayer.get_time() + self.fast * 1000)

                    if self.simultaneousMedia:
                        self.mediaplayer2.set_time(int(self.mediaplayer.get_time() - self.pj[OBSERVATIONS][self.observationId][TIME_OFFSET_SECOND_PLAYER] * 1000))

                elif self.media_list.count() > 1:

                    newTime = (sum(self.duration[0 : self.media_list.index_of_item(self.mediaplayer.get_media()) ]) + self.mediaplayer.get_time()) + self.fast * 1000
                    if newTime < sum(self.duration):
                        # remember if player paused (go previous will start playing)
                        flagPaused = self.mediaListPlayer.get_state() == vlc.State.Paused

                        tot = 0
                        for idx, d in enumerate(self.duration):
                            if newTime >= tot and newTime < tot + d:
                                self.mediaListPlayer.play_item_at_index(idx)
                                app.processEvents()
                                # wait until media is played
                                while True:
                                    if self.mediaListPlayer.get_state() in [vlc.State.Playing, vlc.State.Ended]:
                                        break

                                if flagPaused:
                                    self.mediaListPlayer.pause()

                                self.mediaplayer.set_time( newTime - sum(self.duration[0 : self.media_list.index_of_item(self.mediaplayer.get_media()) ]))

                                break
                            tot += d

                else:
                    self.no_media()

                self.timer_out()
                self.timer_spectro_out()

                # no subtitles
                '''
                logging.debug('no subtitle')
                self.mediaplayer.video_set_spu(0)
                logging.debug('no subtitle done')
                '''


    def reset_activated(self):
        """
        reset video to beginning
        """
        logging.debug("Reset activated")

        if self.playerType == VLC:

            self.pause_video()
            if self.playMode == FFMPEG:

                self.FFmpegGlobalFrame = 0   # position to init
                if self.simultaneousMedia:
                    self.FFmpegGlobalFrame2 = 0   # position to init

                self.ffmpegTimerOut()

            else: #playmode VLC

                self.mediaplayer.set_time(0)

                # second video together
                if self.simultaneousMedia:
                    self.mediaplayer2.set_time(0)

                self.timer_out()
                self.timer_spectro_out()


    def changedFocusSlot(self, old, now):
        """
        connect events filter when app gains focus
        """
        logging.debug("focus changed")

        if window.focusWidget():
            window.focusWidget().installEventFilter(self)

        '''
        if app.focusWidget():
            app.focusWidget().installEventFilter(self)
        '''


if __name__=="__main__":
    #multiprocessing.freeze_support()

    app = QApplication(sys.argv)

    # splashscreen
    if not options.nosplashscreen:
        start = time.time()
        splash = QSplashScreen(QPixmap(os.path.dirname(os.path.realpath(__file__)) + "/splash.png"))
        splash.show()
        splash.raise_()
        while time.time() - start < 1:
            time.sleep(0.001)
            app.processEvents()

    availablePlayers = []

    # load VLC
    import vlc
    if vlc.dll is None:
        logging.critical("VLC media player not found")
        QMessageBox.critical(None, programName, "This program requires the VLC media player.<br>Go to http://www.videolan.org/vlc",
             QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)
        sys.exit(1)

    availablePlayers.append(VLC)

    logging.info("VLC version {}".format(vlc.libvlc_get_version().decode("utf-8")))
    if vlc.libvlc_get_version().decode("utf-8") < VLC_MIN_VERSION:
        QMessageBox.critical(None, programName, "The VLC media player seems very old ({}).<br>Go to http://www.videolan.org/vlc to update it".format(
            vlc.libvlc_get_version()), QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)

        logging.critical("The VLC media player seems old ({}). Go to http://www.videolan.org/vlc to update it".format(vlc.libvlc_get_version()))
        sys.exit(2)

    # check FFmpeg
    ffmpeg_bin = check_ffmpeg_path()
    if not ffmpeg_bin:
        sys.exit(3)

    # check matplotlib
    if not FLAG_MATPLOTLIB_INSTALLED:
        QMessageBox.warning(None, programName,
                            ("""Some functions (plot events and spectrogram) require the Matplotlib module."""
                             """<br>See <a href="http://matplotlib.org">http://matplotlib.org</a>"""),
                            QMessageBox.Ok | QMessageBox.Default, QMessageBox.NoButton)

    app.setApplicationName(programName)
    window = MainWindow(availablePlayers, ffmpeg_bin)

    if args:
        logging.debug("args[0]: " + os.path.abspath(args[0]))
        window.open_project_json(os.path.abspath(args[0]))
        if len(args) > 1:
            logging.debug("opening observation args[1]: " + args[1])
            window.open_observation_by_id(args[1])

    window.show()
    window.raise_()

    # connect events filter when app focus changes
    app.focusChanged.connect(window.changedFocusSlot)

    if not options.nosplashscreen:
        splash.finish(window)

    sys.exit(app.exec_())
