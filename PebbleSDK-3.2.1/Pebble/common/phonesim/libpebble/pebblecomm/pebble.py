#!/usr/bin/env python

import atexit
import binascii
import datetime
import glob
import itertools
import json
import logging as log
import os
import PebbleUtil as util
import png
import random
import re
import sh
import signal
import socket
import speex
import stm32_crc
import struct
import threading
import time
import traceback
import uuid
import WebSocketPebble
import QemuPebble
import zipfile

from AppStore import AppStoreClient
from collections import OrderedDict
from struct import pack, unpack

DEFAULT_PEBBLE_ID = None #Triggers autodetection on unix-like systems
DEFAULT_WEBSOCKET_PORT = 9000
DEBUG_PROTOCOL = False
APP_ELF_PATH = 'build/pebble-app.elf'

class PebbleHardware(object):
    UNKNOWN = 0
    TINTIN_EV1 = 1
    TINTIN_EV2 = 2
    TINTIN_EV2_3 = 3
    TINTIN_EV2_4 = 4
    TINTIN_V1_5 = 5
    BIANCA = 6
    SNOWY_EVT2 = 7
    SNOWY_DVT = 8

    TINTIN_BB = 0xFF
    TINTIN_BB2 = 0xFE
    SNOWY_BB = 0xFD
    SNOWY_BB2 = 0xFC

    PLATFORMS = {
        UNKNOWN: 'unknown',
        TINTIN_EV1: 'aplite',
        TINTIN_EV2: 'aplite',
        TINTIN_EV2_3: 'aplite',
        TINTIN_EV2_4: 'aplite',
        TINTIN_V1_5: 'aplite',
        BIANCA: 'aplite',
        SNOWY_EVT2: 'basalt',
        SNOWY_DVT: 'basalt',
        TINTIN_BB: 'aplite',
        TINTIN_BB2: 'aplite',
        SNOWY_BB: 'basalt',
        SNOWY_BB2: 'basalt',
    }

    PATHS = {
        'unknown': ('',),
        'aplite': ('',),
        'basalt': ('basalt/', ''),
    }

    @classmethod
    def prefixes_for_hardware(cls, hardware):
        platform = cls.PLATFORMS.get(hardware, 'unknown')
        return cls.PATHS[platform]


class PebbleBundle(object):
    MANIFEST_FILENAME = 'manifest.json'
    UNIVERSAL_FILES = {'appinfo.json', 'pebble-js-app.js'}

    STRUCT_DEFINITION = [
            '8s',   # header
            '2B',   # struct version
            '2B',   # sdk version
            '2B',   # app version
            'H',    # size
            'I',    # offset
            'I',    # crc
            '32s',  # app name
            '32s',  # company name
            'I',    # icon resource id
            'I',    # symbol table address
            'I',    # flags
            'I',    # num relocation list entries
            '16s'   # uuid
    ]

    def __init__(self, bundle_path, hardware=PebbleHardware.UNKNOWN):
        self.hardware = hardware
        bundle_abs_path = os.path.abspath(bundle_path)
        if not os.path.exists(bundle_abs_path):
            raise Exception("Bundle does not exist: " + bundle_path)

        self.zip = zipfile.ZipFile(bundle_abs_path)
        self.path = bundle_abs_path
        self.manifest = None
        self.header = None
        self._zip_contents = set(self.zip.namelist())

        self.app_metadata_struct = struct.Struct(''.join(self.STRUCT_DEFINITION))
        self.app_metadata_length_bytes = self.app_metadata_struct.size

        self.print_pbl_logs = False

    def get_real_path(self, path):
        if path in self.UNIVERSAL_FILES:
            return path
        else:
            prefixes = PebbleHardware.prefixes_for_hardware(self.hardware)
            for prefix in prefixes:
                real_path = prefix + path
                if real_path in self._zip_contents:
                    return real_path
            return None


    def get_manifest(self):
        if (self.manifest):
            return self.manifest

        if self.MANIFEST_FILENAME not in self.zip.namelist():
            raise Exception("Could not find {}; are you sure this is a PebbleBundle?".format(self.MANIFEST_FILENAME))

        self.manifest = json.loads(self.zip.read(self.get_real_path(self.MANIFEST_FILENAME)))
        return self.manifest

    def get_app_metadata(self):
        if (self.header):
            return self.header

        app_manifest = self.get_manifest()['application']

        app_bin = self.zip.open(app_manifest['name']).read()

        header = app_bin[0:self.app_metadata_length_bytes]
        values = self.app_metadata_struct.unpack(header)
        self.header = {
                'sentinel' : values[0],
                'struct_version_major' : values[1],
                'struct_version_minor' : values[2],
                'sdk_version_major' : values[3],
                'sdk_version_minor' : values[4],
                'app_version_major' : values[5],
                'app_version_minor' : values[6],
                'app_size' : values[7],
                'offset' : values[8],
                'crc' : values[9],
                'app_name' : values[10].rstrip('\0'),
                'company_name' : values[11].rstrip('\0'),
                'icon_resource_id' : values[12],
                'symbol_table_addr' : values[13],
                'flags' : values[14],
                'num_relocation_entries' : values[15],
                'uuid' : uuid.UUID(bytes=values[16])
        }
        return self.header

    def close(self):
        self.zip.close()

    def is_firmware_bundle(self):
        return 'firmware' in self.get_manifest()

    def is_app_bundle(self):
        return 'application' in self.get_manifest()

    def has_resources(self):
        return 'resources' in self.get_manifest()

    def has_worker(self):
        return 'worker' in self.get_manifest()

    def has_javascript(self):
        return 'js' in self.get_manifest()

    def get_firmware_info(self):
        if not self.is_firmware_bundle():
            return None

        return self.get_manifest()['firmware']

    def get_application_info(self):
        if not self.is_app_bundle():
            return None

        return self.get_manifest()['application']

    def get_resources_info(self):
        if not self.has_resources():
            return None

        return self.get_manifest()['resources']

    def get_worker_info(self):
        if not self.is_app_bundle() or not self.has_worker():
            return None

        return self.get_manifest()['worker']

    def get_app_path(self):
        return self.get_real_path(self.get_application_info()['name'])

    def get_resource_path(self):
        return self.get_real_path(self.get_resources_info()['name'])

    def get_worker_path(self):
        return self.get_real_path(self.get_worker_info()['name'])


class ScreenshotSync():
    timeout = 60
    SCREENSHOT_OK = 0
    SCREENSHOT_MALFORMED_COMMAND = 1
    SCREENSHOT_OOM_ERROR = 2

    def __init__(self, pebble, endpoint, progress_callback):
        self.marker = threading.Event()
        self.data = ''
        self.have_read_header = False
        self.length_received = 0
        self.progress_callback = progress_callback
        pebble.register_endpoint(endpoint, self.message_callback)

    # Received a reply message from the watch. We expect several of these...
    def message_callback(self, endpoint, data):
        if not self.have_read_header:
            data = self.read_header(data)
            self.have_read_header = True

        self.data += data
        self.length_received += len(data) * 8 # in bits
        self.progress_callback(float(self.length_received)/self.total_length)
        if self.length_received >= self.total_length:
            self.marker.set()

    def read_header(self, data):
        image_header = struct.Struct("!BIII")
        header_len = image_header.size
        header_data = data[:header_len]
        data = data[header_len:]
        response_code, version, self.width, self.height = \
          image_header.unpack(header_data)

        if response_code is not ScreenshotSync.SCREENSHOT_OK:
            raise PebbleError(None, "Pebble responded with nonzero response "
                "code %d, signaling an error on the watch side." %
                response_code)

        if version is not 1:
            raise PebbleError(None, "Received unrecognized image format "
                "version %d from watch. Maybe your libpebble is out of "
                "sync with your firmware version?" % version)

        self.total_length = self.width * self.height
        return data

    def get_data_array(self):
        """ splits data in pure binary into a 2D array of bits of length N """
        # break data into bytes
        data_bytes_iter = (ord(ch) for ch in self.data)

        # separate each byte into 8 one-bit entries - IE, 0xf0 --> [1,1,1,1,0,0,0,0]
        data_bits_iter = (byte >> bit_order & 0x01
            for byte in data_bytes_iter for bit_order in xrange(8))

        # pack 1-d bit array of size w*h into h arrays of size w, pad w/ zeros
        output_bitmap = []
        while True:
            try:
                new_row = []
                for _ in xrange(self.width):
                    new_row.append(data_bits_iter.next())
                output_bitmap.append(new_row)
            except StopIteration:
                # add part of the last row anyway
                if new_row:
                    output_bitmap.append(new_row)
                return output_bitmap

    def get_data(self):
        try:
            self.marker.wait(timeout=self.timeout)
            return png.from_array(self.get_data_array(), mode='L;1')
        except:
            raise PebbleError(None, "Timed out... Is the Pebble phone app connected/direct BT connection up?")


class CoreDumpSync():
    timeout_sec = 180

    # See the structure definitions at the top of tintin/src/fw/kernel/core_dump.c for documentation on the format
    #  of the binary core dump file, the core dump download protocol, and error codes
    response_codes = {
        "COREDUMP_OK": 0,
        "MALFORMED_COMMAND": 1,
        "ALREADY_IN_PROGRESS": 2,
        "DOES_NOT_EXIST": 3,
        "CORRUPTED": 4,
    }

    COREDUMP_CMD_REQ_CORE_DUMP_IMAGE = 0
    COREDUMP_CMD_RSP_CORE_DUMP_IMAGE_INFO = 1
    COREDUMP_CMD_RSP_CORE_DUMP_IMAGE_DATA = 2
    COREDUMP_TRANSACTION_ID = 0x42

    def __init__(self, pebble, endpoint, progress_callback):
        self.marker = threading.Event()
        self.data = ''
        self.have_read_header = False
        self.length_received = 0
        self.progress_callback = progress_callback
        self.error_code = 0;
        pebble.register_endpoint(endpoint, self.message_callback)

    # Received a reply message from the watch. We expect several of these...
    def message_callback(self, endpoint, data):
        if not self.have_read_header:
            self.read_header(data)
            self.have_read_header = True
            return

        data_header = struct.Struct("!BBI")
        header_len = data_header.size
        header_data = data[:header_len]
        data = data[header_len:]
        op_code, transaction_id, byte_offset = data_header.unpack(header_data)

        if op_code != CoreDumpSync.COREDUMP_CMD_RSP_CORE_DUMP_IMAGE_DATA:
            self.error_code = -1
            raise PebbleError(None, "Pebble responded with invalid opcode: %d" % (op_code))

        if transaction_id != CoreDumpSync.COREDUMP_TRANSACTION_ID:
            self.error_code = -1
            raise PebbleError(None, "Pebble responded with invalid transaction id %d" % (transaction_id))

        if byte_offset != self.length_received:
            self.error_code = -1
            raise PebbleError(None, "Expected next data with byte offset 0x%x but got byte offset 0x%x" %
                              (self.length_received, byte_offset))

        self.data += data
        self.length_received += len(data)
        print "received 0x%x bytes, length received: 0x%x" % (len(data), self.length_received)
        self.progress_callback(float(self.length_received) / self.total_length)
        if self.length_received >= self.total_length:
            self.marker.set()

    def read_header(self, data):
        core_dump_header = struct.Struct("!BBBI")
        header_len = core_dump_header.size
        header_data = data[:header_len]
        data = data[header_len:]
        op_code, transaction_id, response_code, self.total_length = \
            core_dump_header.unpack(header_data)

        if response_code == self.response_codes["DOES_NOT_EXIST"]:
            raise PebbleError(None, "No coredumps found on watch")

        print "total length of core dump: 0x%x" % (self.total_length)

        if op_code != 1:
            self.error_code = -1
            raise PebbleError(None, "Pebble responded with invalid opcode: %d" % (op_code))

        if transaction_id != self.COREDUMP_TRANSACTION_ID:
            self.error_code = -1
            raise PebbleError(None, "Pebble responded with invalid transaction id: %d" % (transaction_id))

        if response_code != self.response_codes["COREDUMP_OK"]:
            self.error_code = response_code
            raise PebbleError(None, "Pebble responded with nonzero response "
                "code %d, signaling an error on the watch side." % response_code)

        return data

    def get_data(self):
        if self.error_code != 0:
            raise PebbleError(None, "Received error code %d from Pebble" % (self.error_code))
        try:
            self.marker.wait(timeout=self.timeout_sec)
            if self.length_received < self.total_length:
                raise PebbleError(None, "Timed out... Is the Pebble phone app connected/direct BT connection up?")
            return self.data
        except:
            print "Got Error"
            raise PebbleError(None, "Timed out... Is the Pebble phone app connected/direct BT connection up?")
        return None

class AudioSync():

    MSG_ID_START = 0x01
    MSG_ID_DATA = 0x02
    MSG_ID_STOP = 0x03

    def __init__(self, pebble, endpoint, timeout=60):
        self.timeout = timeout
        self.marker = threading.Event()
        self.recording = False
        pebble.register_endpoint(endpoint, self.packet_callback)

    def packet_callback(self, endpoint, data):
        packet_id, = unpack('B', data[0])
        if packet_id == AudioSync.MSG_ID_START:
            self.process_start_packet(data)
        elif packet_id == AudioSync.MSG_ID_DATA:
            self.process_data_packet(data)
        elif packet_id == AudioSync.MSG_ID_STOP:
            self.process_stop_packet(data)

    def process_start_packet(self, data):
        _, _, encoder_id, self.sample_rate, _ = unpack('<BHBIH', data[:10])
        if encoder_id == 1:
            print 'Receiving audio data... Encoded with Speex {}'.format(data[10:30].strip())
        self.frames = []
        self.recording = True

    def process_data_packet(self, data):
        index = 4
        while index < len(data):
            frame_length, = unpack('B', data[index])
            index += 1
            if self.recording:
                self.frames.append(data[index:index + frame_length])
            index += frame_length

    def process_stop_packet(self, data):
        self.marker.set()
        self.recording = False

    def get_data(self):
        try:
            self.marker.wait(self.timeout)
            return self.frames, self.sample_rate
        except:
            raise PebbleError(None, "Timed out... Is the Pebble phone app connected/direct BT connection up?")

class EndpointSync():
    def __init__(self, pebble, endpoint, timeout=10):
        self.marker = threading.Event()
        self.timeout = timeout
        pebble.register_endpoint(endpoint, self.callback)

    def callback(self, endpoint, response):
        self.data = response
        self.marker.set()

    def get_data(self):
        try:
            self.marker.wait(timeout=self.timeout)
            return self.data
        except:
            raise PebbleError(None, "Timed out... Is the Pebble phone app connected/direct BT connection up?")

class QemuEndpointSync():
    timeout = 10

    def __init__(self, pebble, endpoint_id):
        self.marker = threading.Event()
        pebble.register_qemu_endpoint(endpoint_id, self.callback)

    def callback(self, endpoint, response):
        self.data = response
        self.marker.set()

    def get_data(self):
        try:
            self.marker.wait(timeout=self.timeout)
            return self.data
        except:
            raise PebbleError(None, "Timed out... Is QEMU connected?")

class PebbleError(Exception):
    def __init__(self, id, message):
        self._id = id
        self._message = message

    def __str__(self):
        return "%s (ID:%s)" % (self._message, self._id)

class Pebble(object):
    """
    A connection to a Pebble watch; data and commands may be sent
    to the watch through an instance of this class.
    """

    endpoints = {
            "TIME": 11,
            "VERSION": 16,
            "PHONE_VERSION": 17,
            "SYSTEM_MESSAGE": 18,
            "MUSIC_CONTROL": 32,
            "PHONE_CONTROL": 33,
            "APPLICATION_MESSAGE": 48,
            "LAUNCHER": 49,
            "APPLICATION_LIFECYCLE": 52,
            "LOGS": 2000,
            "PING": 2001,
            "LOG_DUMP": 2002,
            "RESET": 2003,
            "APP": 2004,
            "APP_LOGS": 2006,
            "NOTIFICATION": 3000,
            "EXTENSIBLE_NOTIFS": 3010,
            "RESOURCE": 4000,
            "APP_MANAGER": 6000,
            "APP_FETCH": 6001,
            "SCREENSHOT": 8000,
            "COREDUMP": 9000,
            "BLOB_DB": 45531,
            "PUTBYTES": 48879,
            "AUDIO": 10000,
    }

    log_levels = {
            0: "*",
            1: "E",
            50: "W",
            100: "I",
            200: "D",
            250: "V"
    }


    @staticmethod
    def AutodetectDevice():
        if os.name != "posix": #i.e. Windows
            raise NotImplementedError("Autodetection is only implemented on UNIX-like systems.")

        pebbles = glob.glob("/dev/tty.Pebble????-SerialPortSe")

        if len(pebbles) == 0:
            raise PebbleError(None, "Autodetection could not find any Pebble devices")
        elif len(pebbles) > 1:
            log.warn("Autodetect found %d Pebbles; using most recent" % len(pebbles))
            #NOTE: Not entirely sure if this is the correct approach
            pebbles.sort(key=lambda x: os.stat(x).st_mtime, reverse=True)

        id = pebbles[0][15:19]
        log.info("Autodetect found a Pebble with ID %s" % id)
        return id



    def __init__(self, id = None):
        self.id = id
        self._app_log_enabled = False
        self._connection_type = None
        self._ser = None
        self._read_thread = None
        self._alive = True
        self._ws_client = None
        self._endpoint_handlers = {}
        self._internal_endpoint_handlers = {
            self.endpoints["TIME"]: self._get_time_response,
            self.endpoints["VERSION"]: self._version_response,
            self.endpoints["PHONE_VERSION"]: self._phone_version_response,
            self.endpoints["SYSTEM_MESSAGE"]: self._system_message_response,
            self.endpoints["MUSIC_CONTROL"]: self._music_control_response,
            self.endpoints["LOGS"]: self._log_response,
            self.endpoints["PING"]: self._ping_response,
            self.endpoints["EXTENSIBLE_NOTIFS"]: self._notification_response,
            self.endpoints["APP_LOGS"]: self._app_log_response,
            self.endpoints["APP_MANAGER"]: self._appbank_status_response,
            self.endpoints["SCREENSHOT"]: self._screenshot_response,
            self.endpoints["COREDUMP"]: self._coredump_response,
            self.endpoints["AUDIO"]: self._audio_response,
            self.endpoints["BLOB_DB"]: self._blob_db_response,
        }
        self._qemu_endpoint_handlers = {}
        self._qemu_internal_endpoint_handlers = {
            QemuPebble.QemuProtocol_VibrationNotification: self._qemu_vibration_notification,
        }
        self.pebble_protocol_reassembly_buffer = ''
        self.watch_fw_version = None

    def init_reader(self):
        try:
            log.debug("Initializing reader thread")
            self._read_thread = threading.Thread(target=self._reader)
            self._read_thread.setDaemon(True)
            self._read_thread.start()
            log.debug("Reader thread loaded on tid %s" % self._read_thread.name)
        except PebbleError:
            raise PebbleError(id, "Failed to connect to Pebble")
        except:
            raise

    def get_watch_fw_version(self):
        if (self.watch_fw_version is not None):
            return self.watch_fw_version

        version_info = self.get_versions()
        cur_version = version_info['normal_fw']['version']

        # remove the v and split on '.' and '-'
        pieces = re.split("[\.-]", cur_version[1:])
        major = pieces[0]
        minor = pieces[1]

        self.watch_fw_version = [int(major), int(minor)]

        return self.watch_fw_version

    def connect_via_serial(self, id = None):
        self._connection_type = 'serial'

        if id != None:
            self.id = id
        if self.id is None:
            self.id = Pebble.AutodetectDevice()

        import serial
        devicefile = "/dev/tty.Pebble{}-SerialPortSe".format(self.id)
        log.debug("Attempting to open %s as Pebble device %s" % (devicefile, self.id))
        self._ser = serial.Serial(devicefile, 115200, timeout=1)
        self.init_reader()

    def connect_via_lightblue(self, pair_first = False):
        self._connection_type = 'lightblue'

        from LightBluePebble import LightBluePebble
        self._ser = LightBluePebble(self.id, pair_first)
        signal.signal(signal.SIGINT, self._exit_signal_handler)
        atexit.register(self._exit_signal_handler)
        self.init_reader()

    def connect_via_websocket(self, host, port=DEFAULT_WEBSOCKET_PORT):
        self._connection_type = 'websocket'

        # Remove endpoint handlers that we should not respond to
        # (the mobile app will already do this and we should not interfere)
        endpoints_to_remove = ["PHONE_VERSION"]
        for endpoint_name in endpoints_to_remove:
            key = self.endpoints[endpoint_name]
            if key in self._internal_endpoint_handlers:
                del self._internal_endpoint_handlers[key]

        WebSocketPebble.enableTrace(False)
        self._ser = WebSocketPebble.create_connection(host, port, timeout=1, connect_timeout=5)
        self.init_reader()

    def connect_via_qemu(self, host_and_port):
        self._connection_type = 'qemu'

        (host, port) = host_and_port.split(':')
        port = int(port)
        self._ser = QemuPebble.QemuPebble(host, port, timeout=1, connect_timeout=5)
        self._ser.enable_trace(True)
        self._ser.connect()
        self.init_reader()

    def _exit_signal_handler(self, *args):
        log.warn("Disconnecting before exiting...")
        self.disconnect()
        time.sleep(1)
        os._exit(0)

    def __del__(self):
        try:
            self._ser.close()
        except:
            pass

    def _parse_received_pebble_protocol_data(self):
        while True:
            if len(self.pebble_protocol_reassembly_buffer) < 4:
                return
            header = self.pebble_protocol_reassembly_buffer[0:4]
            tail = self.pebble_protocol_reassembly_buffer[4:]
            size, endpoint = unpack("!HH", header)
            if len(tail) < size:
                return
            payload = tail[0:size]
            self.pebble_protocol_reassembly_buffer = self.pebble_protocol_reassembly_buffer[4 + size:]

            if endpoint in self._internal_endpoint_handlers:
                payload = self._internal_endpoint_handlers[endpoint](endpoint, payload)

            if endpoint in self._endpoint_handlers:
                self._endpoint_handlers[endpoint](endpoint, payload)

    def _reader(self):
        try:
            while self._alive:
                source, endpoint, resp = self._recv_message()
                #reading message if socket is closed causes exceptions

                if resp is None or source is None:
                    # ignore message
                    pass

                elif source == 'ws':
                    if endpoint in ['status', 'phoneInfo']:
                        # phone -> sdk message
                        self._ws_client.handle_response(endpoint, resp)
                    elif endpoint == 'log':
                        log.info(resp)
                    elif endpoint == 'watchConnectionStatusUpdate':
                        watch_connected = resp
                        if watch_connected and self._app_log_enabled:
                            self.app_log_enable()

                elif source == 'qemu':
                    if endpoint in self._qemu_internal_endpoint_handlers:
                        resp = self._qemu_internal_endpoint_handlers[endpoint](endpoint, resp)

                    if endpoint in self._qemu_endpoint_handlers and resp is not None:
                        self._qemu_endpoint_handlers[endpoint](endpoint, resp)

                elif source == 'watch':
                    self.pebble_protocol_reassembly_buffer += resp
                    self._parse_received_pebble_protocol_data()

                else:
                    raise ValueError('Unknown source "%s"' % source)

        except Exception as e:
            import traceback
            log.info(traceback.format_exc())

            if type(e) is PebbleError:
                log.info(e)

            else:
                traceback.print_exc()
                log.info("%s: %s" % (type(e), e))
                log.error("Lost connection to Pebble")
                self._alive = False

            # os._exit(-1)


    def _pack_message_data(self, lead, parts):
        pascal = map(lambda x: x[:255], parts)
        d = pack("b" + reduce(lambda x,y: str(x) + "p" + str(y), map(lambda x: len(x) + 1, pascal)) + "p", lead, *pascal)
        return d

    def _build_message(self, endpoint, data):
        return pack("!HH", len(data), endpoint)+data

    def _send_message(self, endpoint, data, callback = None):
        if endpoint not in self.endpoints:
            raise PebbleError(self.id, "Invalid endpoint specified")

        msg = self._build_message(self.endpoints[endpoint], data)

        if DEBUG_PROTOCOL:
            log.debug('>>> ' + msg.encode('hex'))

        self._ser.write(msg)

    def _recv_message(self):
        if self._connection_type != 'serial':
            try:
                source, endpoint, resp, data = self._ser.read()
                if resp is None:
                    return None, None, None
            except TypeError as e:
                log.debug("ws read error...", e.message)
                # the lightblue process has likely shutdown and cannot be read from
                self._alive = False
                return None, None, None
        else:
            data = self._ser.read(4)
            if len(data) == 0:
                return (None, None, None)
            elif len(data) < 4:
                raise PebbleError(self.id, "Malformed response with length "+str(len(data)))
            size, _ = unpack("!HH", data)
            resp = data + self._ser.read(size)
            endpoint = 'Pebble Protocol'
            source = 'watch'
        if DEBUG_PROTOCOL:
            log.debug("Got message for endpoint %s of length %d" % (endpoint, len(resp)))
            log.debug('<<< ' + (data + resp).encode('hex'))
        return (source, endpoint, resp)

    def register_endpoint(self, endpoint_name, func):
        if endpoint_name not in self.endpoints:
            raise PebbleError(self.id, "Invalid endpoint specified")

        endpoint = self.endpoints[endpoint_name]
        self._endpoint_handlers[endpoint] = func

    def register_qemu_endpoint(self, endpoint_id, func):
        self._qemu_endpoint_handlers[endpoint_id] = func

    def notification_sms(self, sender, body):

        """Send a 'SMS Notification' to the displayed on the watch."""

        ts = str(int(time.time())*1000)
        parts = [sender, body, ts]
        self._send_message("NOTIFICATION", self._pack_message_data(1, parts))

    def notification_email(self, sender, subject, body):

        """Send an 'Email Notification' to the displayed on the watch."""

        ts = str(int(time.time())*1000)
        parts = [sender, body, ts, subject]
        self._send_message("NOTIFICATION", self._pack_message_data(0, parts))

    def test_add_notification(self, title = "notification!"):

        notification = Notification(self, title)
        notification.actions.append(Notification.Action(0x01, "GENERIC", "action!"))
        notification.actions.append(Notification.Action(0x02, "DISMISS", "Dismiss!"))
        notification.send()
        return notification

    def set_nowplaying_metadata(self, track, album, artist):

        """Update the song metadata displayed in Pebble's music app."""

        parts = [artist[:30], album[:30], track[:30]]
        self._send_message("MUSIC_CONTROL", self._pack_message_data(16, parts))

    def screenshot(self, progress_callback):
        self._send_message("SCREENSHOT", "\x00")
        return ScreenshotSync(self, "SCREENSHOT", progress_callback).get_data()

    def coredump(self, progress_callback):
        session = CoreDumpSync(self, "COREDUMP", progress_callback);
        self._send_message("COREDUMP", "%c%c" % (CoreDumpSync.COREDUMP_CMD_REQ_CORE_DUMP_IMAGE, CoreDumpSync.COREDUMP_TRANSACTION_ID))
        return session.get_data()

    def get_versions(self, async = False):

        """
        Retrieve a summary of version information for various software
        (firmware, bootloader, etc) running on the watch.
        """

        self._send_message("VERSION", "\x00")

        if not async:
            return EndpointSync(self, "VERSION").get_data()


    def list_apps_by_uuid(self, async=False):
        """Returns the apps installed on the Pebble as a list of Uuid objects."""

        data = pack("b", 0x05)
        self._send_message("APP_MANAGER", data)
        if not async:
            return EndpointSync(self, "APP_MANAGER").get_data()

    def describe_app_by_uuid(self, uuid, uuid_is_string=True, async = False):
        """Returns a dictionary that describes the installed app with the given uuid."""

        if uuid_is_string:
            uuid = uuid.decode('hex')
        elif type(uuid) is uuid.UUID:
            uuid = uuid.bytes
        # else, assume it's a byte array

        data = pack("b", 0x06) + str(uuid)
        self._send_message("APP_MANAGER", data)

        if not async:
            return EndpointSync(self, "APP_MANAGER").get_data()

    def current_running_uuid(self, async = False):
        data = pack("b", 0x07)
        self._send_message("APP_MANAGER", data)
        if not async:
            return EndpointSync(self, "APP_MANAGER").get_data()


    def get_appbank_status(self, async = False):

        """
        Retrieve a list of all installed watch-apps.

        This is particularly useful when trying to locate a
        free app-bank to use when installing a new watch-app.
        """
        self._send_message("APP_MANAGER", "\x01")

        if not async:
            apps = EndpointSync(self, "APP_MANAGER").get_data()
            return apps if type(apps) is dict else { 'apps': [] }

    def remove_app(self, appid, index, async=False):

        """Remove an installed application from the target app-bank."""

        data = pack("!bII", 2, appid, index)
        self._send_message("APP_MANAGER", data)

        if not async:
            return EndpointSync(self, "APP_MANAGER").get_data()

    def remove_app_by_uuid(self, uuid_to_remove, uuid_is_string=True, async = False):
        """Remove an installed application by UUID. Returns a string indicating status."""

        if uuid_is_string:
            uuid_to_remove = uuid_to_remove.decode('hex')
        elif type(uuid_to_remove) is uuid.UUID:
            uuid_to_remove = uuid_to_remove.bytes
        # else, assume it's a byte array

        data = pack("b", 0x02) + str(uuid_to_remove)
        self._send_message("APP_MANAGER", data)

        if not async:
            return EndpointSync(self, "APP_MANAGER").get_data()

    def get_time(self, async = False):

        """Retrieve the time from the Pebble's RTC."""

        self._send_message("TIME", "\x00")

        if not async:
            return EndpointSync(self, "TIME").get_data()

    def record(self, name="recording"):

        """Decode and store audio data streamed from Pebble"""

        try:
            frames, sample_rate = AudioSync(self, "AUDIO").get_data()
            speex.store_data(frames, name, sample_rate)
            print "Recording stored in", name
        except PebbleError as e:
            print e

    def set_time(self, timestamp):

        """Set the time stored in the target Pebble's RTC."""

        data = pack("!bL", 2, timestamp)
        self._send_message("TIME", data)


    def install_bundle_ws(self, bundle_path):
        self._ws_client = WSClient()
        f = open(bundle_path, 'r')
        data = f.read()
        self._ser.write(data, ws_cmd=WebSocketPebble.WS_CMD_BUNDLE_INSTALL)
        self._ws_client.listen()
        while not self._ws_client._received and not self._ws_client._error:
            pass
        if self._ws_client._topic == 'status' \
                and self._ws_client._response == 0:
            log.info("Installation successful")
            return True
        log.debug("WS Operation failed with response %s" %
                                        self._ws_client._response)
        log.error("Failed to install %s" % repr(bundle_path))
        return False

    def is_phone_info_available(self):
        return self._connection_type == 'websocket'

    def get_phone_info(self):
        if self._connection_type != 'websocket':
            raise Exception("Not connected via websockets - cannot get phone info")

        self._ws_client = WSClient()
        # The first byte is reserved for future use as a protocol version ID
        #  and must be 0 for now.
        data = pack("!b", 0)
        self._ser.write(data, ws_cmd=WebSocketPebble.WS_CMD_PHONE_INFO)
        self._ws_client.listen()
        while not self._ws_client._received and not self._ws_client._error:
          pass
        if self._ws_client._topic == 'phoneInfo':
          return self._ws_client._response
        else:
          log.error('get_phone_info: Unexpected response to "%s"' % self._ws_client._topic)
          return 'Unknown'

    def install_app_pebble_protocol_2_x(self, pbw_path, launch_on_install=True):
        device_version = self.get_versions()
        hardware_version = device_version['normal_fw']['hardware_platform']

        bundle = PebbleBundle(pbw_path, hardware_version)
        if not bundle.is_app_bundle():
            raise PebbleError(self.id, "This is not an app bundle")

        app_metadata = bundle.get_app_metadata()
        self.remove_app_by_uuid(app_metadata['uuid'].bytes, uuid_is_string=False)
        time.sleep(1)  # If this isn't here then the next operation may timeout if the app is already installed.

        apps = self.get_appbank_status()
        if not apps:
            raise PebbleError(self.id, "could not obtain app list; try again")

        first_free = 0
        for app in apps["apps"]:
            if app["index"] == first_free:
                first_free += 1
        if first_free == apps["banks"]:
            raise PebbleError(self.id, "All %d app banks are full" % apps["banks"])
        log.debug("Attempting to add app to bank %d of %d" % (first_free, apps["banks"]))

        # Install the app code
        app_info = bundle.get_application_info()
        binary = bundle.zip.read(bundle.get_app_path())
        if bundle.has_resources():
            resources = bundle.zip.read(bundle.get_resource_path())
        else:
            resources = None

        client = PutBytesClient(self, first_free, "BINARY", binary)
        self.register_endpoint("PUTBYTES", client.handle_message)
        client.init()
        while not client._done and not client._error:
            time.sleep(0.5)
        if client._error:
            raise PebbleError(self.id, "Failed to send application binary %s/pebble-app.bin" % pbw_path)

        # Install the resources
        if resources:
            client = PutBytesClient(self, first_free, "RESOURCES", resources)
            self.register_endpoint("PUTBYTES", client.handle_message)
            client.init()
            while not client._done and not client._error:
                time.sleep(0.5)
            if client._error:
                raise PebbleError(self.id, "Failed to send application resources %s/app_resources.pbpack" % pbw_path)

        # Is there a worker to install?
        worker_info = bundle.get_worker_info()
        if worker_info is not None:
          binary = bundle.zip.read(bundle.get_worker_path())
          client = PutBytesClient(self, first_free, "WORKER", binary)
          self.register_endpoint("PUTBYTES", client.handle_message)
          client.init()
          while not client._done and not client._error:
              time.sleep(0.5)
          if client._error:
              raise PebbleError(self.id, "Failed to send worker binary %s/%s" % (pbw_path, worker_info['name']))


        time.sleep(2)
        self._add_app(first_free)
        time.sleep(2)

        if launch_on_install:
            self.launcher_message(app_metadata['uuid'].bytes, "RUNNING", uuid_is_string=False, async=True)

        # If we have not thrown an exception, we succeeded
        return True

    def install_app_pebble_protocol_3_x(self, pbw_path, launch_on_install=True):

        bundle = PebbleBundle(pbw_path)
        if not bundle.is_app_bundle():
            raise PebbleError(self.id, "This is not an app bundle")

        app_metadata = bundle.get_app_metadata()

        metadata_blob = AppMetadata(
            self,
            app_metadata['uuid'],
            app_metadata['flags'],
            0,
            app_metadata['app_version_major'],
            app_metadata['app_version_minor'],
            app_metadata['sdk_version_major'],
            app_metadata['sdk_version_minor'],
            0,
            0,
            app_metadata['app_name']
        )

        resp = metadata_blob.send()
        if resp is not "SUCCESS":
            print "Error: " + resp

        # launch application
        self.launcher_message(app_metadata['uuid'].bytes, "RUNNING", uuid_is_string=False, async = True)

        # listen for app fetch
        app_fetch = EndpointSync(self, "APP_FETCH").get_data()

        command, app_uuid, app_id = unpack("<B16sI", app_fetch)
        uuid_str = str(uuid.UUID(bytes=app_uuid))

        # send ACK, no response comes back
        resp = pack("BB", 1, 1) # APP_FETCH_INSTALL_RESPONSE, SUCCESS
        self._send_message("APP_FETCH", resp)

        time.sleep(1)

        self.install_app_binaries_pebble_protocol(pbw_path, app_id)

    def install_app_pebble_protocol(self, pbw_path, launch_on_install=True):

        # determine if 2.x or 3.x
        watch_fw_version = self.get_watch_fw_version()
        # print "tyler"
        if (watch_fw_version[0] >= 3):
            self.install_app_pebble_protocol_3_x(pbw_path, launch_on_install)
        else:
            self.install_app_pebble_protocol_2_x(pbw_path, launch_on_install)

        # If we have not thrown an exception, we succeeded
        return True

    def install_app_binaries_pebble_protocol(self, pbw_path, app_id):
        device_version = self.get_versions()
        hardware_version = device_version['normal_fw']['hardware_platform']

        bundle = PebbleBundle(pbw_path, hardware_version)
        if not bundle.is_app_bundle():
            raise PebbleError(self.id, "This is not an app bundle")

        # Install the app code
        binary = bundle.zip.read(bundle.get_app_path())
        if bundle.has_resources():
            resources = bundle.zip.read(bundle.get_resource_path())
        else:
            resources = None

        client = PutBytesClient(self, app_id, "BINARY", binary, has_cookie=True)
        self.register_endpoint("PUTBYTES", client.handle_message)
        client.init()
        while not client._done and not client._error:
            time.sleep(0.5)
        if client._error:
            raise PebbleError(self.id, "Failed to send application binary %s/pebble-app.bin" % pbw_path)

        # Install the resources
        if resources:
            client = PutBytesClient(self, app_id, "RESOURCES", resources, has_cookie=True)
            self.register_endpoint("PUTBYTES", client.handle_message)
            client.init()
            while not client._done and not client._error:
                time.sleep(0.5)
            if client._error:
                raise PebbleError(self.id, "Failed to send application resources %s/app_resources.pbpack" % pbw_path)

        # Is there a worker to install?
        worker_info = bundle.get_worker_info()
        if worker_info is not None:
          binary = bundle.zip.read(bundle.get_worker_path())
          client = PutBytesClient(self, app_id, "WORKER", binary, has_cookie=True)
          self.register_endpoint("PUTBYTES", client.handle_message)
          client.init()
          while not client._done and not client._error:
              time.sleep(0.5)
          if client._error:
              raise PebbleError(self.id, "Failed to send worker binary %s/%s" % (pbw_path, bundle.get_worker_path()))

        # If we have not thrown an exception, we succeeded
        return True

    def install_app(self, pbw_path, launch_on_install=True, direct=False):

        """Install an app bundle (*.pbw) to the target Pebble."""

        # FIXME: One problem here is that install_bundle_ws will return True/False
        # but install_app_pebble_protocol will return True or throw an exception.
        # We should catch, report to user and return False.
        if not direct and self._connection_type == 'websocket':
            return self.install_bundle_ws(pbw_path)
        else:
            return self.install_app_pebble_protocol(pbw_path, launch_on_install)

    def timeline_add_pin(self):
        fmt = "<16s16sIHBHBBBB"
        pin_id = uuid.uuid4()
        pin = struct.pack(fmt,
            pin_id.get_bytes(), # UUID
            "\x00",             # parent id
            int(time.time()),   # timestamp
            0,                  # duration
            2,                  # type (pin)
            0,                  # flags
            0,                  # pin layout
            0,                  # view layout
            0,                  # num attributes
            0)                  # num actions
        print "adding pin {}".format(pin_id.hex)
        return self._raw_blob_db_insert("PIN", pin_id.get_bytes(), pin)

    def timeline_remove_pin(self, uuid, uuid_is_string=True):
        if uuid_is_string:
            uuid = uuid.decode('hex')
        elif type(uuid) is uuid.UUID:
            uuid = uuid.bytes
        # else, assume it's a byte array
        return self._raw_blob_db_delete("PIN", uuid)

    def install_app_metadata(self, in_uuid, flags):
        rand_name = uuid.uuid4().get_hex()[0:6] # generate random name
        uuid_bytes = util.convert_to_bytes(in_uuid)
        app = struct.pack(
            "<16sIIHHBB96s",
            uuid_bytes,
            flags,              # info_flags
            17,                 # total_size
            0,                  # app_version
            0,                  # sdk_version
            0,                  # app_face_bg_color
            0,                  # app_face_template_id
            rand_name)          # random name
        return self._raw_blob_db_insert("APP", uuid_bytes, app)

    def remove_app_metadata(self, uuid):
        uuid_bytes = util.convert_to_bytes(uuid)
        return self._raw_blob_db_delete("APP", uuid_bytes)

    def blob_db_insert(self, db, key, value):
        key_bytes = util.convert_to_bytes(key)
        value_bytes = util.convert_to_bytes(value)
        return self._raw_blob_db_insert(db, key_bytes, value_bytes)

    def blob_db_delete(self, db, key):
        key_bytes = util.convert_to_bytes(key)
        return self._raw_blob_db_delete(db, key_bytes)

    def blob_db_clear(self, db):
        return self._raw_blob_db_clear(db)

    def test_reminder_db(self, timedelta=20):
        reminder = Reminder(self, "Reminder", int(time.time()) - time.timezone + timedelta)
        reminder.send()
        print reminder.id
        return reminder

    def _raw_blob_db_insert(self, db, key, value):
        db = BlobDB(db)
        data = db.insert(key, value)
        self._send_message("BLOB_DB", data)
        return EndpointSync(self, "BLOB_DB").get_data()

    def _raw_blob_db_delete(self, db, key):
        db = BlobDB(db)
        data = db.delete(key)
        self._send_message("BLOB_DB", data)
        return EndpointSync(self, "BLOB_DB").get_data()

    def _raw_blob_db_clear(self, db):
        db = BlobDB(db)
        data = db.clear()
        self._send_message("BLOB_DB", data)
        return EndpointSync(self, "BLOB_DB").get_data()


    def send_file(self, file_path, name):
        data = open(file_path, 'r').read()
        client = PutBytesClient(self, 0, "FILE", data, name)
        self.register_endpoint("PUTBYTES", client.handle_message)
        client.init()
        while not client._done and not client._error:
            pass
        if client._error:
            raise PebbleError(self.id, "Failed to send file %s" % file_path)
        log.info("File transfer succesful")

    def install_firmware(self, pbz_path, recovery=False):

        """Install a firmware bundle to the target watch."""

        resources = None
        with zipfile.ZipFile(pbz_path) as pbz:
            binary = pbz.read("tintin_fw.bin")
            if not recovery:
                resources = pbz.read("system_resources.pbpack")

        self.system_message("FIRMWARE_START")
        time.sleep(2)

        if resources:
            client = PutBytesClient(self, 0, "SYS_RESOURCES", resources)
            self.register_endpoint("PUTBYTES", client.handle_message)
            client.init()
            while not client._done and not client._error:
                pass
            if client._error:
                raise PebbleError(self.id, "Failed to send firmware resources %s/system_resources.pbpack" % pbz_path)


        client = PutBytesClient(self, 0, "RECOVERY" if recovery else "FIRMWARE", binary)
        self.register_endpoint("PUTBYTES", client.handle_message)
        client.init()
        while not client._done and not client._error:
            pass
        if client._error:
            raise PebbleError(self.id, "Failed to send firmware binary %s/tintin_fw.bin" % pbz_path)

        log.info("Installation successful")
        self.system_message("FIRMWARE_COMPLETE")

    def launcher_message(self, app_uuid, key_value, uuid_is_string = True, async = False):
        """ send an appication message to launch or kill a specified application"""

        launcher_keys = {
                "RUN_STATE_KEY": 1,
        }

        launcher_key_values = {
                "NOT_RUNNING": b'\x00',
                "RUNNING": b'\x01'
        }

        if key_value not in launcher_key_values:
            raise PebbleError(self.id, "not a valid application message")

        if uuid_is_string:
            app_uuid = app_uuid.decode('hex')
        elif type(app_uuid) is uuid.UUID:
            app_uuid = app_uuid.bytes
        #else we can assume it's a byte array

        amsg = AppMessage()

        # build and send a single tuple-sized launcher command
        app_message_tuple = amsg.build_tuple(launcher_keys["RUN_STATE_KEY"], "UINT", launcher_key_values[key_value])
        app_message_dict = amsg.build_dict([app_message_tuple])
        packed_message = amsg.build_message(app_message_dict, "PUSH", app_uuid)
        self._send_message("LAUNCHER", packed_message)

        # wait for either ACK or NACK response
        if not async:
            return EndpointSync(self, "LAUNCHER").get_data()

    def app_message_send_tuple(self, app_uuid, key, tuple_datatype, tuple_data):

        """  Send a Dictionary with a single tuple to the app corresponding to UUID """

        app_uuid = app_uuid.decode('hex')
        amsg = AppMessage()

        app_message_tuple = amsg.build_tuple(key, tuple_datatype, tuple_data)
        app_message_dict = amsg.build_dict([app_message_tuple])
        packed_message = amsg.build_message(app_message_dict, "PUSH", app_uuid)
        self._send_message("APPLICATION_MESSAGE", packed_message)

    def app_message_send_string(self, app_uuid, key, string):

        """  Send a Dictionary with a single tuple of type CSTRING to the app corresponding to UUID """

        # NULL terminate and pack
        string = string + '\0'
        fmt =  '<' + str(len(string)) + 's'
        string = pack(fmt, string);

        self.app_message_send_tuple(app_uuid, key, "CSTRING", string)

    def app_message_send_uint(self, app_uuid, key, tuple_uint):

        """  Send a Dictionary with a single tuple of type UINT to the app corresponding to UUID """

        fmt = '<' + str(tuple_uint.bit_length() / 8 + 1) + 'B'
        tuple_uint = pack(fmt, tuple_uint)

        self.app_message_send_tuple(app_uuid, key, "UINT", tuple_uint)

    def app_message_send_int(self, app_uuid, key, tuple_int):

        """  Send a Dictionary with a single tuple of type INT to the app corresponding to UUID """

        fmt = '<' + str(tuple_int.bit_length() / 8 + 1) + 'b'
        tuple_int = pack(fmt, tuple_int)

        self.app_message_send_tuple(app_uuid, key, "INT", tuple_int)

    def app_message_send_byte_array(self, app_uuid, key, tuple_byte_array):

        """  Send a Dictionary with a single tuple of type BYTE_ARRAY to the app corresponding to UUID """

        # Already packed, fix endianness
        tuple_byte_array = tuple_byte_array[::-1]

        self.app_message_send_tuple(app_uuid, key, "BYTE_ARRAY", tuple_byte_array)

    def system_message(self, command):

        """
        Send a 'system message' to the watch.

        These messages are used to signal important events/state-changes to the watch firmware.
        """

        commands = {
                "FIRMWARE_AVAILABLE": 0,
                "FIRMWARE_START": 1,
                "FIRMWARE_COMPLETE": 2,
                "FIRMWARE_FAIL": 3,
                "FIRMWARE_UP_TO_DATE": 4,
                "FIRMWARE_OUT_OF_DATE": 5,
                "BLUETOOTH_START_DISCOVERABLE": 6,
                "BLUETOOTH_END_DISCOVERABLE": 7
        }
        if command not in commands:
            raise PebbleError(self.id, "Invalid command \"%s\"" % command)
        data = pack("!bb", 0, commands[command])
        log.debug("Sending command %s (code %d)" % (command, commands[command]))
        self._send_message("SYSTEM_MESSAGE", data)


    def ping(self, cookie = 0xDEC0DE, async = False):

        """Send a 'ping' to the watch to test connectivity."""

        data = pack("!bL", 0, cookie)
        self._send_message("PING", data)

        if not async:
            return EndpointSync(self, "PING").get_data()

    def reset(self, prf=False, coredump=False, factory_reset=False):

        """Reset the watch remotely."""

        has_option_already = False
        for option in [prf, coredump, factory_reset]:
            if option:
                if has_option_already:
                    raise Exception("prf, coredump and factory_reset are"
                                    " mutually exclusive!")
                has_option_already = True

        if prf:
            cmd = "\xFF"  # Recovery Mode
        elif factory_reset:  # Factory Reset
            cmd = "\xFE"
        elif coredump:
            cmd = "\x01"  # Force coredump
        else:
            cmd = "\x00"  # Normal reset
        self._send_message("RESET", cmd)

    def emu_tap(self, axis='x', direction=1):

        """Send a tap to the watch running in the emulator"""
        axes = {'x': 0, 'y': 1, 'z': 2}
        axis_int = axes.get(axis)
        msg = pack('!bb', axis_int, direction);

        if DEBUG_PROTOCOL:
            log.debug('>>> ' + msg.encode('hex'))

        self._ser.write(msg, protocol=QemuPebble.QemuProtocol_Tap)

    def emu_bluetooth_connection(self, connected=True):

        """Send a bluetooth connection event to the watch running in the emulator"""
        msg = pack('!b', connected);

        if DEBUG_PROTOCOL:
            log.debug('>>> ' + msg.encode('hex'))

        self._ser.write(msg, protocol=QemuPebble.QemuProtocol_BluetoothConnection)


    def emu_compass(self, heading=0, calib=2):

        """Send a compass event to the watch running in the emulator"""
        msg = pack('!Ib', heading, calib);

        if DEBUG_PROTOCOL:
            log.debug('>>> ' + msg.encode('hex'))

        self._ser.write(msg, protocol=QemuPebble.QemuProtocol_Compass)


    def emu_battery(self, pct=80, charging=True):

        """Send battery info to the watch running in the emulator"""
        msg = pack('!bb', pct, charging);

        if DEBUG_PROTOCOL:
            log.debug('>>> ' + msg.encode('hex'))

        self._ser.write(msg, protocol=QemuPebble.QemuProtocol_Battery)


    def emu_accel(self, motion=None, filename=None):

        """Send accel data to the watch running in the emulator
           The caller is responsible for validating that 'motion' is a valid string
        """
        MAX_ACCEL_SAMPLES = 255
        if motion == 'tilt_left':
            samples = [[-500, 0, -900], [-900, 0, -500], [-1000, 0, 0],]

        elif motion == 'tilt_right':
            samples = [[500, 0, -900], [900, 0, -500], [1000, 0, 0]]

        elif motion == 'tilt_forward':
            samples = [[0, 500, -900], [0, 900, -500], [0, 1000, 0],]

        elif motion == 'tilt_back':
            samples = [[0, -500, -900], [0, -900, -500], [0, -1000, 0],]

        elif motion.startswith('gravity'):
            # The format expected here is 'gravity<sign><axis>', where sign can be '-', or '+' and
            # axis can be 'x', 'y', or 'z'
            if motion[len('gravity')] == '+':
                amount = 1000
            else:
                amount = -1000
            axis_letter = motion[len('gravity-')]
            axis_index = {'x':0, 'y':1, 'z':2}[axis_letter]
            samples = [[0, 0, 0]]
            samples[0][axis_index] = amount

        elif motion == 'custom':
            if (filename is None):
                raise Exception("No filename specified");
            samples = []
            with open(filename) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        samples.append([int(x) for x in line.split(',')])
        else:
            raise Exception("Unsupported accel motion: '%s'" % (motion))

        if len(samples) > MAX_ACCEL_SAMPLES:
            raise Exception("Cannot send %d samples. The max number of accel samples that can be "
                      "sent at a time is %d." % (len(samples), MAX_ACCEL_SAMPLES))
        msg = pack('!b', len(samples))
        for sample in samples:
            sample_data = pack('!hhh', sample[0], sample[1], sample[2])
            msg += sample_data

        if DEBUG_PROTOCOL:
            log.debug('>>> ' + msg.encode('hex'))

        self._ser.write(msg, protocol=QemuPebble.QemuProtocol_Accel)

        response = QemuEndpointSync(self, QemuPebble.QemuProtocol_Accel).get_data()
        samples_avail = struct.Struct("!H").unpack(response)
        print "Success: room for %d more samples" % (samples_avail)


    def emu_button(self, button_id):

        """Send a short button press to the watch running in the emulator.
        0: back, 1: up, 2: select, 3: down """

        button_state = 1 << button_id;
        while True:
            # send the press immediately followed by the release
            msg = pack('!b', button_state);

            if DEBUG_PROTOCOL:
                log.debug('>>> ' + msg.encode('hex'))

            self._ser.write(msg, protocol=QemuPebble.QemuProtocol_Button)
            if button_state == 0:
                break;
            button_state = 0


    def _qemu_vibration_notification(self, endpoint, data):
        on, = unpack("!b", data)
        print "Vibration: %s" % ("on" if on else "off")

    def dump_logs(self, generation_number):
        """Dump the saved logs from the watch.

        Arguments:
        generation_number -- The genration to dump, where 0 is the current boot and 3 is the oldest boot.
        """

        if generation_number > 3:
            raise Exception("Invalid generation number %u, should be [0-3]" % generation_number)

        log.info('=== Generation %u ===' % generation_number)

        class LogDumpClient(object):
            def __init__(self, pebble):
                self.done = False
                self._pebble = pebble

            def parse_log_dump_response(self, endpoint, data):
                if (len(data) < 5):
                    log.warn("Unable to decode log dump message (length %d is less than 8)" % len(data))
                    return

                response_type, response_cookie = unpack("!BI", data[:5])
                if response_type == 0x81:
                    self.done = True
                    return
                elif response_type != 0x80 or response_cookie != cookie:
                    log.info("Received unexpected message with type 0x%x cookie %u expected 0x80 %u" %
                        (response_type, response_cookie, cookie))
                    self.done = True
                    return

                timestamp, str_level, filename, linenumber, message = self._pebble._parse_log_response(data[5:])

                timestamp_str = datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')

                log.info("{} {} {}:{}> {}".format(str_level, timestamp_str, filename, linenumber, message))

        client = LogDumpClient(self)
        self.register_endpoint("LOG_DUMP", client.parse_log_dump_response)

        import random
        cookie = random.randint(0, pow(2, 32) - 1)
        self._send_message("LOG_DUMP", pack("!BBI", 0x10, generation_number, cookie))

        while not client.done:
            time.sleep(1)

    def app_log_enable(self):
        self._app_log_enabled = True
        log.info("Enabling application logging...")
        self._send_message("APP_LOGS", pack("!B", 0x01))

    def app_log_disable(self):
        self._app_log_enabled = False
        log.info("Disabling application logging...")
        self._send_message("APP_LOGS", pack("!B", 0x00))

    def disconnect(self):

        """Disconnect from the target Pebble."""

        self._alive = False
        self._ser.close()

    def set_print_pbl_logs(self, value):
        self.print_pbl_logs = value

    def _add_app(self, index):
        data = pack("!bI", 3, index)
        self._send_message("APP_MANAGER", data)

    def _screenshot_response(self, endpoint, data):
        return data

    def _coredump_response(self, endpoint, data):
        return data

    def _audio_response(self, endpoint, data):
        return data

    def _ping_response(self, endpoint, data):
        # Ping responses can either be 5 bytes or 6 bytes long.
        # The format is [ 1 byte command | 4 byte cookie | 1 byte idle flag (optional) ]

        # We only care about the cookie, so just strip the idle flag before calling unpack
        restype, retcookie = unpack("!bL", data[0:5])
        return retcookie

    def _notification_response(self, endpoint, data):
        # pass in the "pebble" object
        Notification.response(self, endpoint, data)

    def _get_time_response(self, endpoint, data):
        restype, timestamp = unpack("!bL", data)
        return timestamp

    def _system_message_response(self, endpoint, data):
        if len(data) == 2:
            log.info("Got system message %s" % repr(unpack('!bb', data)))
        elif len(data) == 3:
            log.info("Got system message %s" % repr(unpack('!bbb', data)))
        else:
            log.info("Got 'unknown' system message...")

    def _parse_log_response(self, log_message_data):
        timestamp, level, msgsize, linenumber = unpack("!IBBH", log_message_data[:8])
        filename = (log_message_data[8:24].split("\0")[0]).decode('utf-8', 'ignore')
        message = log_message_data[24:24+msgsize].decode('utf-8', 'ignore')

        str_level = self.log_levels[level] if level in self.log_levels else "?"

        return timestamp, str_level, filename, linenumber, message

    def _log_response(self, endpoint, data):
        if (len(data) < 8):
            log.warn("Unable to decode log message (length %d is less than 8)" % len(data))
            return

        if self.print_pbl_logs:
            timestamp, str_level, filename, linenumber, message = self._parse_log_response(data)

            log.info("{} {} {} {} {}".format(timestamp, str_level, filename, linenumber, message))

    def _print_crash_message(self, crashed_uuid, crashed_pc, crashed_lr):
        # Read the current projects UUID from it's appinfo.json. If we can't do this or the uuid doesn't match
        # the uuid of the crashed app we don't print anything.
        import os, sys
        sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'pebble'))
        from PblProjectCreator import check_project_directory, PebbleProjectException
        try:
            check_project_directory()
        except PebbleProjectException:
            # We're not in the project directory
            return

        with open('appinfo.json', 'r') as f:
            try:
                app_info = json.load(f)
                app_uuid = uuid.UUID(app_info['uuid'])
            except ValueError as e:
                log.warn("Could not look up debugging symbols.")
                log.warn("Failed parsing appinfo.json")
                log.warn(str(e))
                return

        if (app_uuid != crashed_uuid):
            # Someone other than us crashed, just bail
            return


        if not os.path.exists(APP_ELF_PATH):
            log.warn("Could not look up debugging symbols.")
            log.warn("Could not find ELF file: %s" % APP_ELF_PATH)
            log.warn("Please try rebuilding your project")
            return


        def print_register(register_name, addr_str):
            if (addr_str[0] == '?') or (int(addr_str, 16) > 0x20000):
                # We log '???' when the reigster isn't available

                # The firmware translates app crash addresses to be relative to the start of the firmware
                # image. We filter out addresses that are higher than 128k since we know those higher addresses
                # are most likely from the firmware itself and not the app

                result = '???'
            else:
                result = sh.arm_none_eabi_addr2line(addr_str, exe=APP_ELF_PATH,
                                                    _tty_out=False).strip()

            log.warn("%24s %10s %s", register_name + ':', addr_str, result)

        print_register("Program Counter (PC)", crashed_pc)
        print_register("Link Register (LR)", crashed_lr)


    def _app_log_response(self, endpoint, data):
        if (len(data) < 8):
            log.warn("Unable to decode log message (length %d is less than 8)" % len(data))
            return

        app_uuid = uuid.UUID(bytes=data[0:16])
        timestamp, str_level, filename, linenumber, message = self._parse_log_response(data[16:])

        log.info("{} {}:{} {}".format(str_level, filename, linenumber, message))

        # See if the log message we printed matches the message we print when we crash. If so, try to provide
        # some additional information by looking up the filename and linenumber for the symbol we crasehd at.
        m = re.search('App fault! ({[0-9a-fA-F\-]+}) PC: (\S+) LR: (\S+)', message)
        if m:
            crashed_uuid_str = m.group(1)
            crashed_uuid = uuid.UUID(crashed_uuid_str)

            self._print_crash_message(crashed_uuid, m.group(2), m.group(3))

    def _appbank_status_response(self, endpoint, data):
        def unpack_uuid(data):
            UUID_FORMAT = "{}{}{}{}-{}{}-{}{}-{}{}-{}{}{}{}{}{}"
            uuid = unpack("!bbbbbbbbbbbbbbbb", data)
            uuid = ["%02x" % (x & 0xff) for x in uuid]
            return UUID_FORMAT.format(*uuid)
        apps = {}
        restype, = unpack("!b", data[0])

        if restype == 1:
            apps["banks"], apps_installed = unpack("!II", data[1:9])
            apps["apps"] = []

            appinfo_size = 78
            offset = 9
            for i in xrange(apps_installed):
                app = {}
                try:
                    app["id"], app["index"], app["name"], app["company"], app["flags"], app["version"] = \
                            unpack("!II32s32sIH", data[offset:offset+appinfo_size])
                    app["name"] = app["name"].replace("\x00", "")
                    app["company"] = app["company"].replace("\x00", "")
                    apps["apps"] += [app]
                except:
                    if offset+appinfo_size > len(data):
                        log.warn("Couldn't load bank %d; remaining data = %s" % (i,repr(data[offset:])))
                    else:
                        raise
                offset += appinfo_size

            return apps

        elif restype == 2:
            message_id = unpack("!I", data[1:])
            message_id = int(''.join(map(str, message_id)))

            # FIXME: These response strings only apply to responses to app remove (0x2) commands
            # If you receive a 0x2 message in response to a app install (0x3) message you actually
            # need to use a different mapping.
            #
            # The mapping for responses to 0x3 commands is as follows...
            # APP_AVAIL_SUCCESS = 1
            # APP_AVAIL_BANK_IN_USE = 2
            # APP_AVAIL_INVALID_COMMAND = 3
            # APP_AVAIL_GENERAL_FAILURE = 4
            #
            # However, we only ever check responses to app remove commands in this file, so just
            # use those mappings, as below. I'm not sure how to fix this going forward, as we don't
            # have a way of figuring out which response type we're getting without making this
            # code stateful, which I don't really want to do...
            app_install_message = {
                1: "success",
                2: "no app in bank",
                3: "install id mismatch",
                4: "invalid command",
                5: "general failure"
            }

            return app_install_message[message_id]

        elif restype == 5:
            apps_installed = unpack("!I", data[1:5])[0]
            uuids = []

            uuid_size = 16
            offset = 5
            for i in xrange(apps_installed):
                uuid = unpack_uuid(data[offset:offset+uuid_size])
                offset += uuid_size
                uuids.append(uuid)
            return uuids

        elif restype == 6:
            app = {}
            app["version"], app["name"], app["company"] = unpack("H32s32s", data[1:])
            app["name"] = app["name"].replace("\x00", "")
            app["company"] = app["company"].replace("\x00", "")
            return app

        elif restype == 7:
            uuid = unpack_uuid(data[1:17])
            return uuid

        else:
            return restype

    def _version_response(self, endpoint, data):
        fw_names = {
                0: "normal_fw",
                1: "recovery_fw"
        }

        resp = {}
        for i in xrange(2):
            fwver_size = 47
            offset = i*fwver_size+1
            fw = {}
            fw["timestamp"],fw["version"],fw["commit"],fw["is_recovery"], \
                    fw["hardware_platform"],fw["metadata_ver"] = \
                    unpack("!i32s8s?Bb", data[offset:offset+fwver_size])

            fw["version"] = fw["version"].replace("\x00", "")
            fw["commit"] = fw["commit"].replace("\x00", "")

            fw_name = fw_names[i]
            resp[fw_name] = fw

        resp["bootloader_timestamp"],resp["hw_version"],resp["serial"] = \
                unpack("!L9s12s", data[95:120])

        resp["hw_version"] = resp["hw_version"].replace("\x00","")

        btmac_hex = binascii.hexlify(data[120:126])
        resp["btmac"] = ":".join([btmac_hex[i:i+2].upper() for i in reversed(xrange(0, 12, 2))])

        return resp

    def _phone_version_response(self, endpoint, data):
        session_cap = {
                "GAMMA_RAY" : 0x80000000,
        }
        remote_cap = {
                "TELEPHONY" : 16,
                "SMS" : 32,
                "GPS" : 64,
                "BTLE" : 128,
                "CAMERA_REAR" : 256,
                "ACCEL" : 512,
                "GYRO" : 1024,
                "COMPASS" : 2048,
        }
        os = {
                "UNKNOWN" : 0,
                "IOS" : 1,
                "ANDROID" : 2,
                "OSX" : 3,
                "LINUX" : 4,
                "WINDOWS" : 5,
        }

        # Then session capabilities, android adds GAMMA_RAY and it's
        # the only session flag so far
        session = session_cap["GAMMA_RAY"]

        # Then phone capabilities, android app adds TELEPHONY and SMS,
        # and the phone type (we know android works for now)
        remote = remote_cap["TELEPHONY"] | remote_cap["SMS"] | os["ANDROID"]

        # Version 2 of the phone version response.
        response_vers = 2
        major = 2
        minor = 3
        bugfix = 0

        msg = pack("!biIIbbbb", 1, -1, session, remote, response_vers,
                   major, minor, bugfix)
        self._send_message("PHONE_VERSION", msg);

    def _music_control_response(self, endpoint, data):
        event, = unpack("!b", data)

        event_names = {
                1: "PLAYPAUSE",
                4: "NEXT",
                5: "PREVIOUS",
        }

        return event_names[event] if event in event_names else None

    def _blob_db_response(self, endpoint, data):
        db = BlobDB()
        token, resp = unpack("HB", data)
        return db.interpret_response(resp)


class AppMessage(object):
# tools to build a valid app message
    def build_tuple(self, key, data_type, data):
        """ make a single app_message tuple"""
        # available app message datatypes:
        tuple_datatypes = {
                "BYTE_ARRAY": b'\x00',
                "CSTRING": b'\x01',
                "UINT": b'\x02',
                "INT": b'\x03'
        }

        # build the message_tuple
        app_message_tuple = OrderedDict([
                ("KEY", pack('<L', key)),
                ("TYPE", tuple_datatypes[data_type]),
                ("LENGTH", pack('<H', len(data))),
                ("DATA", data)
        ])

        return app_message_tuple

    def build_dict(self, tuple_of_tuples):
        """ make a dictionary from a list of app_message tuples"""
        # note that "TUPLE" can refer to 0 or more tuples. Tuples must be correct endian-ness already
        tuple_count = len(tuple_of_tuples)
        # make the bytearray from the flattened tuples
        tuple_total_bytes = ''.join(item for item in itertools.chain(*[x.values() for x in tuple_of_tuples]))
        # now build the dict
        app_message_dict = OrderedDict([
                ("TUPLECOUNT", pack('B', tuple_count)),
                ("TUPLE", tuple_total_bytes)
        ])
        return app_message_dict

    def build_message(self, dict_of_tuples, command, uuid, transaction_id=b'\x00'):
        """ build the app_message intended for app with matching uuid"""
        # NOTE: uuid must be a byte array
        # available app_message commands:
        app_messages = {
                "PUSH": b'\x01',
                "REQUEST": b'\x02',
                "ACK": b'\xFF',
                "NACK": b'\x7F'
        }
        # finally build the entire message
        app_message = OrderedDict([
                ("COMMAND", app_messages[command]),
                ("TRANSACTIONID", transaction_id),
                ("UUID", uuid),
                ("DICT", ''.join(dict_of_tuples.values()))
        ])
        return ''.join(app_message.values())


class WSClient(object):
    states = {
      "IDLE": 0,
      "LISTENING": 1,
    }

    def __init__(self):
      self._state = self.states["IDLE"]
      self._response = None
      self._topic = None
      self._received = False
      self._error = False
      # Call the timeout handler after the timeout.
      self._timer = threading.Timer(90.0, self.timeout)
      self._timer.setDaemon(True)

    def timeout(self):
      if (self._state != self.states["LISTENING"]):
        log.error("Timeout triggered when not listening")
        return
      self._error = True
      self._received = False
      self._state = self.states["IDLE"]

    def listen(self):
      self._state = self.states["LISTENING"]
      self._received = False
      self._error = False
      self._timer.start()

    def handle_response(self, topic, response):
      if self._state != self.states["LISTENING"]:
        log.debug("Unexpected status message")
        self._error = True

      self._timer.cancel()
      self._topic = topic
      self._response = response;
      self._received = True

class PutBytesClient(object):
    states = {
            "NOT_STARTED": 0,
            "WAIT_FOR_TOKEN": 1,
            "IN_PROGRESS": 2,
            "COMMIT": 3,
            "COMPLETE": 4,
            "FAILED": 5
    }

    transfer_types = {
            "FIRMWARE": 1,
            "RECOVERY": 2,
            "SYS_RESOURCES": 3,
            "RESOURCES": 4,
            "BINARY": 5,
            "FILE": 6,
            "WORKER": 7,
    }

    def __init__(self, pebble, index, transfer_type, buffer, filename="", has_cookie=False):
        if len(filename) > 255:
            raise Exception("Filename too long (>255 chars) " + filename)

        self._pebble = pebble
        self._state = self.states["NOT_STARTED"]
        self._transfer_type = self.transfer_types[transfer_type]
        self._buffer = buffer
        self._index = index
        self._done = False
        self._error = False
        self._filename = filename + '\0'
        self._has_cookie = has_cookie

    def init(self):
        if self._has_cookie:
            self._transfer_type = self._transfer_type | (1 << 7)
            data = pack("!BIBI", 1, len(self._buffer), self._transfer_type, self._index)
        else:
            data = pack("!BIBB%ds" % (len(self._filename)), 1, len(self._buffer), self._transfer_type, self._index, self._filename)

        self._pebble._send_message("PUTBYTES", data)
        self._state = self.states["WAIT_FOR_TOKEN"]

    def wait_for_token(self, resp):
        res, = unpack("!b", resp[0])
        if res != 1:
            log.error("init failed with code %d" % res)
            self._error = True
            return
        self._token, = unpack("!I", resp[1:])
        self._left = len(self._buffer)
        self._state = self.states["IN_PROGRESS"]
        self.send()

    def in_progress(self, resp):
        res, = unpack("!b", resp[0])
        if res != 1:
            self.abort()
            return
        if self._left > 0:
            self.send()
            log.info("Sent %d of %d bytes" % (len(self._buffer)-self._left, len(self._buffer)))
        else:
            self._state = self.states["COMMIT"]
            self.commit()

    def commit(self):
        data = pack("!bII", 3, self._token & 0xFFFFFFFF, stm32_crc.crc32(self._buffer))
        self._pebble._send_message("PUTBYTES", data)

    def handle_commit(self, resp):
        res, = unpack("!b", resp[0])
        if res != 1:
            self.abort()
            return
        self._state = self.states["COMPLETE"]
        self.complete()

    def complete(self):
        data = pack("!bI", 5, self._token & 0xFFFFFFFF)
        self._pebble._send_message("PUTBYTES", data)

    def handle_complete(self, resp):
        res, = unpack("!b", resp[0])
        if res != 1:
            self.abort()
            return
        self._done = True

    def abort(self):
        msgdata = pack("!bI", 4, self._token & 0xFFFFFFFF)
        self._pebble._send_message("PUTBYTES", msgdata)
        self._error = True

    def send(self):
        datalen =  min(self._left, 2000)
        rg = len(self._buffer)-self._left
        msgdata = pack("!bII", 2, self._token & 0xFFFFFFFF, datalen)
        msgdata += self._buffer[rg:rg+datalen]
        self._pebble._send_message("PUTBYTES", msgdata)
        self._left -= datalen

    def handle_message(self, endpoint, resp):
        if self._state == self.states["WAIT_FOR_TOKEN"]:
            self.wait_for_token(resp)
        elif self._state == self.states["IN_PROGRESS"]:
            self.in_progress(resp)
        elif self._state == self.states["COMMIT"]:
            self.handle_commit(resp)
        elif self._state == self.states["COMPLETE"]:
            self.handle_complete(resp)

# Attributes and Actions are currently defined outside of Notifications
# and TimelineItems because they're common to both
class Attribute(object):

    """
    An attribute for a Notification, TimelineItem or an Action.

    Possible attribute IDs are:
        - TITLE
        - SUBTITLE
        - BODY
        - TINY_ICON
        - SMALL_ICON
        - TBD_ICON
        - ANCS_ID
        - ACTION_CANNED_RESPONSE
        - PIN_ICON (TimelineItem only)
        - SHORT_TITLE (TimelineItem only)
    """

    attribute_table = {
        "TITLE": 0x01,
        "SUBTITLE": 0x02,
        "BODY": 0x03,
        "TINY_ICON": 0x04,
        "SMALL_ICON": 0x05,
        "TBD_ICON": 0x06,
        "ANCS_ID": 0x07,
        "ACTION_CANNED_RESPONSE": 0x08,
        "SHORT_TITLE": 0x09,
        "PIN_ICON": 0x0a
    }

    def __init__(self, id, content):
        self.id = id
        self.content = content

    def pack(self):
        fmt = "<BH" + str(len(self.content)) + "s"
        return pack(fmt, self.attribute_table[self.id], len(self.content), self.content)

class Action(object):

    """An Action that can be added to a notification or timeline item.

    Possible action types are:
        - ANCS_DISMISS
        - GENERIC
        - RESPONSE
        - DISMISS
    The following action types are available for timeline items:
        - HTTP
        - SNOOZE
        - OPEN_WATCHAPP
        - EMPTY (no actions)
    """

    action_table = {
        "ANCS_DISMISS": 0x01,
        "GENERIC": 0x02,
        "RESPONSE": 0x03,
        "DISMISS": 0x04,
        "HTTP": 0x05,
        "SNOOZE": 0x06,
        "OPEN_WATCHAPP": 0x07,
        "EMPTY": 0x08
    }

    def __init__(self, id, type, title, attributes=None):
        """
        Create an Action object.

        id is a number that must be unique for the notification.
        type is one of the action types listed in the class docstring.
        """

        self.id = id
        self.type = type
        self.title = title
        if attributes:
            self.attributes = attributes
        else:
            self.attributes = []

    def pack(self):
        fmt = "<BBB"
        attributes = [Attribute("TITLE", self.title)]
        data = pack(fmt, self.id, self.action_table[self.type], len(attributes))
        for attribute in attributes:
            data += attribute.pack()
        return data

class Notification(object):

    """A custom notification to send to the watch.
    """

    commands = {
        "INVOKE_NOTIFICATION_ACTION": 0x02,
        "WATCH_ACK_NACK": 0x10,
        "PHONE_ACK_NACK": 0x11,
    }

    phone_action = {
        "ACK": 0x00,
        "NACK": 0x01
    }

    def __init__(self, pebble, title, attributes=None, actions=None):

        """Create a Notification object.

        The title argument is provided for convenience. It is simply added to the attribute
        list later.
        """

        self.pebble = pebble
        self.title = title
        self.attributes = attributes if attributes else []
        self.actions = actions if actions else []
        self.notif_id = random.randint(0, 0xFFFFFFFE)


    def send(self, silent=False, utc=True, layout=0x01):

        attributes = [Attribute("TITLE", self.title)] + self.attributes
        header_fmt = "<BBIIIIBBB" # header
        flags = (2 * utc) + silent
        header_data = pack(header_fmt,
            0x00,
            0x01, # add notif
            flags, # flags
            self.notif_id, # notif ID
            0x00000000, # ANCS ID
            int(time.time()), # timestamp
            layout, # layout
            len(attributes),
            len(self.actions))

        attributes_data = "".join([x.pack() for x in attributes])
        actions_data = "".join([x.pack() for x in self.actions])

        data = header_data + attributes_data + actions_data
        self.pebble._send_message("EXTENSIBLE_NOTIFS", data)

    def remove(self):

        """Remove a notification from the watch. Currently not implemented watch-side."""

        # 0x01 is the "remove notification" command
        data = pack("<BI", 0x01, self.notif_id)
        self.pebble._send_message("EXTENSIBLE_NOTIFS", data)

    @classmethod
    def response(cls, pebble, endpoint, data):
        command, = unpack("<B", data[:1])
        log.debug("notification command 0x%x" % command)
        if command == cls.commands["INVOKE_NOTIFICATION_ACTION"]:
            # always respond with ACK
            _, notif_id, action_id = unpack("<BIB", data[:6])
            log.debug("Invoked action 0x%x on notification 0x%x" % (action_id, notif_id))
            # no attributes sent back
            ack_response = pack("<BIBBB", cls.commands["PHONE_ACK_NACK"], notif_id, action_id, cls.phone_action["ACK"], 0)
            pebble._send_message("EXTENSIBLE_NOTIFS", ack_response)
        elif command == cls.commands["WATCH_ACK_NACK"]:
            _, notif_id, resp = unpack("<BIB", data[:6])
            resp_type = "ACK" if resp == 0x00 else "NACK"
            log.debug("%s'd for notification 0x%x" % (resp_type, notif_id))
        else:
            log.debug("notification command 0x%x not recognized" % command)


class TimelineItem(object):

    """A timeline item to send to the watch.
    Timeline items can be reminders, pins, or notifications.
    """

    # ensure these match with the TimelineItemType enum
    # in tintin/src/fw/services/normal/timeline/item.h
    item_type = {
        "NOTIFICATION": 1,
        "PIN": 2,
        "REMINDER": 3,
    }

    def __init__(self, pebble, title, timestamp=int(time.time()), duration=0, type="PIN",
        parent=None, attributes=None, actions=None, is_floating=False, visible=False,
        reminded=False, actioned=False, read=False, layout=0x01):

        """Create a TimelineItem object.

        The title is provided for convenience. It is simply added to the attribute
        list later.
        """

        self.pebble = pebble
        self.title = title
        self.timestamp = timestamp
        self.duration = duration
        self.type = type
        self.attributes = attributes if attributes else []
        self.actions = actions if actions else []
        self.parent = parent if parent else uuid.UUID(int=0)
        self.id = uuid.uuid4()
        self.is_floating = is_floating
        self.visible = visible
        self.reminded = reminded
        self.actioned = actioned
        self.read = read
        self.layout = layout

    def send(self):
        attributes = [Attribute("TITLE", self.title)] + self.attributes
        header_fmt = "<16s16sIHBHBHBB"
        flags = (
            1 << 0 * self.is_floating +
            1 << 1 * self.visible +
            1 << 2 * self.reminded +
            1 << 3 * self.actioned +
            1 << 4 * self.read
            )

        attributes_data = "".join([x.pack() for x in attributes])
        actions_data = "".join([x.pack() for x in self.actions])

        header_data = pack(header_fmt,
            self.id.bytes,
            self.parent.bytes,
            self.timestamp,
            self.duration,
            self.item_type[self.type],
            flags,
            self.layout,
            len(attributes_data) + len(actions_data),
            len(attributes),
            len(self.actions))

        data = header_data + attributes_data + actions_data

        if self.type == "NOTIFICATION":
            self.pebble._send_message("EXTENSIBLE_NOTIFS", data)
        else:
            blobdb = BlobDB(self.type)
            blobdb_data = blobdb.insert(self.id.bytes, data)
            self.pebble._send_message("BLOB_DB", blobdb_data)
            return EndpointSync(self.pebble, "BLOB_DB").get_data()


class AppMetadata(object):

    def __init__(self, pebble, in_uuid, flags, total_size, app_version_major,
        app_version_minor, sdk_version_major, sdk_version_minor, app_face_bg_color,
        app_face_template_id, app_name):

        self.pebble = pebble
        self.in_uuid = in_uuid
        self.flags = flags
        self.total_size = total_size
        self.app_version_major = app_version_major
        self.app_version_minor = app_version_minor
        self.sdk_version_major = sdk_version_major
        self.sdk_version_minor = sdk_version_minor
        self.app_face_bg_color = app_face_bg_color
        self.app_face_template_id = app_face_template_id
        self.app_name = app_name

    def send(self):
        uuid_bytes = util.convert_to_bytes(self.in_uuid)

        data = struct.pack(
            "<16sIIBBBBBB96s",
            uuid_bytes,
            self.flags,
            self.total_size,
            self.app_version_major,
            self.app_version_minor,
            self.sdk_version_major,
            self.sdk_version_minor,
            self.app_face_bg_color,
            self.app_face_template_id,
            self.app_name
        )

        return self.pebble._raw_blob_db_insert("APP", uuid_bytes, data)

class Reminder(TimelineItem):

    """A reminder to pop up on the watch, implemented as a specific type of TimelineItem.
    """

    def __init__(self, pebble, title, timestamp, **kwargs):
        super(Reminder, self).__init__(pebble, title, timestamp,
            0, "REMINDER", **kwargs)


class BlobDB(object):

    dbs = {
            "TEST": 0,
            "PIN": 1,
            "APP": 2,
            "REMINDER": 3
    }

    def __init__(self, db="TEST"):
        self.db_id = self.dbs[db];

    def get_token(self):
        return random.randrange(1, pow(2,16) - 1, 1)

    def insert(self, key, value):
        token = self.get_token()
        data = pack("<BHBB", 0x01, token, self.db_id, len(key)) + str(key) \
                    + pack("<H", len(value)) + str(value)
        return data

    def delete(self, key):
        token = self.get_token()
        data = pack("<BHBB", 0x04, token, self.db_id, len(key)) + str(key)
        return data

    def clear(self):
        token = self.get_token()
        data = pack("<BHB", 0x05, token, self.db_id)
        return data

    def interpret_response(self, code):
        if (code == 1):
            return "SUCCESS"
        else:
            return "ERROR: %d" % (code)
