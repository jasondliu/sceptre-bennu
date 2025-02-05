"""
SCEPTRE Provider for the Real-Time Dynamic Simulator (RTDS).

## Overview

Data is read via C37.118 protocol from Phasor Measurement Unit (PMU) interface on the RTDS GTNET card.

Data is written via GTNET-SKT protocol to the RTDS GTNET card.


## GTNET-SKT protocol for the RTDS

Refer to the Section 7H2 of RTDS Controls Library manual for details
("cc_man.pdf", included with the RSCAD software distribution).

**Overview**
- 32-bit (4 byte) values
- Big-endian (network byte order)
- Integers or Single-precision IEEE 751 Floating-point numbers
- List of ints/floats is packed into packet sent to the GTNET card
- The order, types, and tags they map to is defined in the "GTNET-SKT MULTI" block in the RSCAD project
- GTNET-SKT requires that ALL configured values are written in a single packet

**Channels**
- Max of 30 data points per channel
- Max of 10 channels total (30 * 10 = 300 total points per card)
- Each channel has it's own IP and port (though port could be same)

### !! WARNING !!

Too high of a write rate (>1khz?) will overload and crash the GTNET card!
If this happens, it may need to be reset by flipping the power switch on
the back of the card off, then on. It may also be enough to reset the RTDS
power using the power switch on the front of the rack.

### About writes...

As stated above, GTNET-SKT requires that ALL configured values are written in
a single packet, with the order, types, and tags determined by what's defined
in RSCAD. This is because it relies on the order of tags to unpack the packet
into values.

What this means is all writes to the socket MUST contain ALL values, and
the ORDER of those values in the packet MATTERS! Since SCEPTRE provides the
ability to write to individual tags, e.g. change value of tag 'BREAKER-1'
to a '1', this leaves us in a bit of a pickle.

To work around this issue, the provider will maintain an internal (in-memory)
state of the last known values for each tag. When a write occurs to a tag or
set of tags, the state will be updated with values from the write. Then, the
current (now updated) state will be written to the GTNET-SKT socket. At startup,
the initial state will be populated from the provider config file (.ini).

As far as I know there isn't a way to read the current state of GTNET-SKT points
unless you do some massive hacks involving PMU measurements, which add further
complexity to an already needlessly complicated RSCAD project. So, while this
solution isn't perfect, it's good enough to meet the HARMONIE project objectives
in the short-term.


## CSV files
Data read from the PMUs is saved to CSV files in the directory specified by the
`csv-file-path` configuration option, if `csv-enabled` is True.

CSV header example:
```
sequence,rtds_time,sceptre_time,freq,dfreq,VA_real,VA_angle,VB_real,VB_angle,VC_real,VC_angle,IA_real,IA_angle,IB_real,IB_angle,IC_real,IC_angle,NA_real,NA_angle,NA_real,NA_angle
```

CSV filename example: `PMU1_BUS4-1_25-04-2022_23-49-22.csv`


## Elasticsearch

Data read from the PMUs is exported to the Elasticsearch server specified by the
`elastic-host` configuration option, if `elastic-enabled` is True.

Index name: `rtds-<YYYY.MM.DD>` (e.g. `rtds-2022.04.26`)

### Index mapping

| field                    | type          | example                   | description |
| ------------------------ | ------------- | ------------------------- | ----------- |
| @timestamp               | date          | `2022-04-20:11:22:33.000` | Timestamp from RTDS. |
| rtds_time                | date          | `2022-04-20:11:22:33.000` | Timestamp from RTDS. |
| sceptre_time             | date          | `2022-04-20:11:22:33.000` | Timestamp from SCEPTRE provider (the `power-provider` VM in the emulation). |
| event.ingested           | date          | `2022-04-20:11:22:33.000` | Timestamp of when the data was ingested into Elasticsearch. |
| ecs.version              | keyword       | `8.1.0`                   | [Elastic Common Schema (ECS)](https://www.elastic.co/guide/en/ecs/current/ecs-field-reference.html) version this schema adheres to. |
| agent.type               | keyword       | `rtds-sceptre-provider`   | Type of system providing the data |
| agent.version            | keyword       | `4.0.0`                   | Version of the provider |
| observer.hostname        | keyword       | `power-provider`          | Hostname of the system providing the data. |
| observer.geo.timezone    | keyword       | `America/Denver`          | Timezone of the system providing the data. |
| network.protocol         | keyword       | `dnp3`                    | Network protocol used to retrieve the data. Currently, this will be either `dnp3` or `c37.118`. |
| network.transport        | keyword       | `tcp`                     | Transport layer (Layer 4 of OSI model) protocol used to retrieve the data. Currently, this is usually `tcp`, but it could be `udp` if UDP is used for C37.118 or GTNET-SKT. |
| pmu.name                 | keyword       | `PMU1`                    | Name of the PMU. |
| pmu.label                | keyword       | `BUS4-1`                  | Label for the PMU. |
| pmu.ip                   | ip            | `172.24.9.51`             | IP address of the PMU. |
| pmu.port                 | integer       | `4714`                    | TCP port of the PMU. |
| pmu.id                   | long          | `41`                      | PDC ID of the PMU. |
| measurement.stream       | byte          | `1`                       | Stream ID of this measurement from the PMU. |
| measurement.status       | keyword       | `ok`                      | Status of this measurement from the PMU. |
| measurement.sequence     | unsigned_long | `1001`                    | Sequence of this value in reads from the PMU. |
| measurement.frequency    | float         | `60.06`                   | Nominal system frequency. |
| measurement.dfreq        | float         | `8.835189510136843e-05`   | Rate of change of frequency (ROCOF). |
| measurement.channel      | keyword       | `PHASOR CH 1:VA`          | Channel name of this measurement from the PMU. |
| measurement.phasor.id    | byte          | `0`                       | ID of the phasor. For example, if there are 4 phasors, then the ID of the first phasor will be `0`. |
| measurement.phasor.real  | float         | `132786.5`                | Phase magnitude? |
| measurement.phasor.angle | float         | `-1.5519471168518066`     | Phase angle? |

## Notes
NOTE: in the bennu VM, rtds.py is located in dist-packages.
Just in case you need to modify it on the fly ;)
``/usr/lib/python3/dist-packages/pybennu/providers/power/solvers/rtds.py``

"""
import atexit
import json
import logging
import os
import platform
import re
import socket
import struct
import sys
import threading
import traceback
from configparser import ConfigParser
from datetime import datetime, timezone
from io import TextIOWrapper
from pathlib import Path
from time import sleep
from typing import Any, Dict, List, Optional

from elasticsearch import Elasticsearch, helpers

from pybennu._version import __version__
from pybennu.distributed.provider import Provider
from pybennu.pypmu.synchrophasor.frame import (CommonFrame, DataFrame,
                                               HeaderFrame)
from pybennu.pypmu.synchrophasor.pdc import Pdc

# TODO: rebuilding PMU connections for some reason results
# in no data going to elastic (and maybe CSVs?)
# TODO: support PMU "digital" fields (mm["digital"])
# TODO: support PMU "analog" fields, current handling is a hack for HARMONIE LDRD
# TODO: move CSV writing into threads like Elastic is?
# TODO: push pre-defined type mapping to Elasticsearch when creating index


class RotatingCSVWriter:
    """Writes data to CSV files, creating a new file when a limit is reached."""
    def __init__(
        self,
        name: str,
        csv_dir: Path,
        header: List[str],
        filename_base: str = "rtds_pmu_data",
        rows_per_file: int = 1000000,
        max_files: int = 0
    ):
        self.name = name
        self.csv_dir = csv_dir  # type: Path
        self.header = header  # type: List[str]
        self.filename_base = filename_base
        self.max_rows = rows_per_file
        self.max_files = max_files
        self.files_written = []  # type: List[Path]
        self.rows_written = 0
        self.current_path = None  # type: Optional[Path]
        self.fp = None  # type: Optional[TextIOWrapper]

        self.log = logging.getLogger(f"{self.__class__.__name__} [{self.name}]")
        self.log.setLevel(logging.DEBUG)

        if not self.csv_dir.exists():
            self.csv_dir.mkdir(parents=True, exist_ok=True, mode=0o666)  # rw-rw-rw-

        self.log.info(f"CSV output directory: {self.csv_dir}")
        self.log.info(f"CSV header: {self.header}")

        # ensure data is written on exit
        atexit.register(self._close_file)

        # initial file rotation
        self.rotate()

    def _close_file(self):
        if self.fp and not self.fp.closed:
            self.fp.flush()
            os.fsync(self.fp.fileno())  # ensure data is written to disk
            self.fp.close()

    def rotate(self):
        self._close_file()  # close current CSV before starting new one
        if self.current_path:
            self.log.debug(f"Wrote {self.rows_written} rows and {self.current_path.stat().st_size} bytes to {self.current_path}")
            self.files_written.append(self.current_path)

        timestamp = datetime.utcnow().strftime("%d-%m-%Y_%H-%M-%S")
        filename = f"{self.filename_base}_{timestamp}.csv"
        self.current_path = Path(self.csv_dir, filename)
        self.log.info(f"Rotating CSV file to {self.current_path}")
        if self.current_path.exists():
            self.log.error(f"{self.current_path} already exists! Overwriting...")

        # Set file permissions: User/Group/World Read/Write (rw-rw-rw-)
        self.current_path.touch(mode=0o666, exist_ok=True)
        self.fp = self.current_path.open("w", encoding="utf-8", newline="\n")
        self._emit(self.header)  # Write CSV header
        self.rows_written = 0  # Reset row counter

        if self.max_files and len(self.files_written) > self.max_files:
            oldest = self.files_written.pop(0)  # oldest file is first in list
            self.log.info(f"Removing CSV file {oldest}")
            oldest.unlink()  # delete the file

    def _emit(self, data: list):
        """Write comma-separated list of values."""
        for i, column in enumerate(data):
            self.fp.write(str(column))
            if i < len(data) - 1:
                self.fp.write(",")
        self.fp.write("\n")

    def write(self, data: list):
        """Write data to CSV file."""
        if self.rows_written == self.max_rows:
            self.rotate()

        if len(data) != len(self.header):
            raise RuntimeError(f"length of CSV data ({len(data)}) does not match length of CSV header ({len(self.header)})")
        assert len(data) == len(self.header)

        self._emit(data)
        self.rows_written += 1


class PMU:
    """
    Wrapper class for a Phasor Measurement Unit (PMU).

    Polls for data using C37.118 protocol, utilizing the pypmu library under-the-hood.
    """
    def __init__(self, ip: str, port: int, pdc_id: int, name: str = "", label: str = ""):
        self.ip = ip
        self.port = port
        self.pdc_id = pdc_id
        self.name = name
        self.label = label

        # Configure PDC instance (pypmu.synchrophasor.pdc.Pdc)
        self.pmu = Pdc(self.pdc_id, self.ip, self.port)
        self.pmu_header = None  # type: Optional[HeaderFrame]
        self.pmu_config = None  # type: Optional[CommonFrame]
        self.channel_names = []  # type: List[str]
        self.sequence = 0  # type: int
        self.csv_writer = None  # type: Optional[RotatingCSVWriter]
        self.station_name = ""  # type: str

        # Configure logging
        self.log = logging.getLogger(f"PMU [{str(self)}]")
        self.log.setLevel(logging.DEBUG)
        self.pmu.logger = logging.getLogger(f"Pdc [{str(self)}]")
        # NOTE: pypmu logs a LOT of stuff at DEBUG, leave at INFO
        # unless you're doing deep debugging of the pypmu code.
        self.pmu.logger.setLevel(logging.INFO)
        self.log.info(f"Initialized {repr(self)}")

    def __repr__(self) -> str:
        return f"PMU(ip={self.ip}, port={self.port}, pdc_id={self.pdc_id}, name={self.name}, label={self.label})"

    def __str__(self) -> str:
        if self.name and self.label:
            return f"{self.name}_{self.label}"
        elif self.name:
            return self.name
        else:
            return f"{self.ip}:{self.port}_{self.pdc_id}"

    def run(self):
        """Connect to PMU."""
        self.pmu.run()

        # NOTE (03/30/2022): some SEL PDCs respond to header requests and don't need them
        try:
            self.pmu_header = self.pmu.get_header()  # Get header message from PMU
            self.log.debug(f"PMU header: {self.pmu_header.__dict__}")
        except Exception as ex:
            self.log.warning(f"Failed to get header: {ex} (device may be a SEL PDC, or something else happened)")

        self.pmu_config = self.pmu.get_config()  # Get configuration from PMU
        self.log.debug(f"PMU config: {self.pmu_config.__dict__}")
        if "_station_name" in self.pmu_config.__dict__:
            self.station_name = self.pmu_config.__dict__["_station_name"].strip()
            self.log.info(f"PMU Station Name: {self.station_name}")
        else:
            self.log.warning(f"No station_name from PMU, which is a bit odd")

        # Raw: ["PHASOR CH 1:VA  ", "PHASOR CH 2:VB  ", "PHASOR CH 3:VC  ",
        #       "PHASOR CH 4:IA  ", "PHASOR CH 5:IB  ", "PHASOR CH 6:IC  "]
        # Post-processing: ["VA", "VB", "VC", "IA", "IB", "IC"]
        def _process_name(cn: str) -> str:
            """Strip 'PHASOR CH *' from channel names, so we get a nice 'VA', 'IA', etc."""
            return re.sub(r"PHASOR CH \d\:", "", cn.strip(), re.IGNORECASE | re.ASCII).strip()
        self.channel_names = []  # type: List[str]
        for channel in self.pmu_config.__dict__.get("_channel_names", []):
            if isinstance(channel, list):
                # NOTE (03/30/2022): channel names can be lists of strings instead of strings
                for n in channel:
                    self.channel_names.append(_process_name(n))
            else:
                self.channel_names.append(_process_name(channel))
        self.log.debug(f"Channel names: {self.channel_names}")

    def start(self):
        self.pmu.start()

    def get_data_frame(self) -> Optional[Dict[str, Any]]:
        data = self.pmu.get()  # Keep receiving data

        if not data:
            self.log.error("Failed to get data from PMU!")
            return None

        if not isinstance(data, DataFrame):
            self.log.critical(f"Invalid type {type(data).__name__}. Raw data: {repr(data)}")
            return None

        data_frame = data.get_measurements()

        if data_frame["pmu_id"] != self.pdc_id:
            self.log.warning(f"The received PMU ID {data_frame['pmu_id']} is not same as configured ID {self.pdc_id}")

        return data_frame


class RTDS(Provider):
    """SCEPTRE Provider for the Real-Time Dynamic Simulator (RTDS)."""

    REQUIRED_CONF_KEYS = [  # NOTE: these are checked in ../power_daemon.py
        "server-endpoint", "publish-endpoint", "publish-rate", "rtds-retry-delay", "rtds-rack-ip",
        "rtds-pmu-ips", "rtds-pmu-ports", "rtds-pmu-names", "rtds-pmu-labels", "rtds-pdc-ids",
        "csv-enabled", "csv-file-path", "csv-rows-per-file", "csv-max-files",
        "gtnet-skt-ip", "gtnet-skt-port", "gtnet-skt-protocol", "gtnet-skt-tag-names",
        "gtnet-skt-tag-types", "gtnet-skt-initial-values",
        "elastic-enabled", "elastic-host",
    ]

    def __init__(self, server_endpoint, publish_endpoint, config: ConfigParser, debug: bool = False):
        Provider.__init__(self, server_endpoint, publish_endpoint)
        self.__lock = threading.Lock()
        self.__es_lock = threading.Lock()

        # Load configuration values
        self.config = config  # type: ConfigParser
        self.debug = debug  # type: bool

        self.publish_rate = float(self._conf("publish-rate"))  # type: float

        # RTDS config
        self.retry_delay = float(self._conf("rtds-retry-delay"))  # type: float
        self.rack_ip = self._conf("rtds-rack-ip")  # type: str
        self.pmu_ips = self._conf("rtds-pmu-ips", is_list=True)  # type: List[str]
        self.pmu_names = self._conf("rtds-pmu-names", is_list=True)  # type: List[str]
        self.pmu_ports = self._conf("rtds-pmu-ports", is_list=True, convert=int)  # type: List[int]
        self.pmu_labels = self._conf("rtds-pmu-labels", is_list=True)  # type: List[str]
        self.pdc_ids = self._conf("rtds-pdc-ids", is_list=True, convert=int)  # type: List[int]

        # CSV config
        self.csv_enabled = True if self._conf("csv-enabled").lower() == "true" else False  # type: bool
        self.csv_path = Path(self._conf("csv-file-path")).expanduser().resolve()  # type: Path
        self.csv_rows_per_file = int(self._conf("csv-rows-per-file"))  # type: int
        self.csv_max_files = int(self._conf("csv-max-files"))  # type: int

        # GTNET-SKT config
        self.gtnet_skt_ip = self._conf("gtnet-skt-ip")  # type: str
        self.gtnet_skt_port = int(self._conf("gtnet-skt-port"))  # type: int
        self.gtnet_skt_protocol = self._conf("gtnet-skt-protocol").lower()  # type: str
        self.gtnet_skt_tag_names = self._conf("gtnet-skt-tag-names", is_list=True)  # type: List[str]
        self.gtnet_skt_tag_types = self._conf("gtnet-skt-tag-types", is_list=True, convert=lambda x: x.lower())  # type: List[str]
        self.gtnet_skt_initial_values = self._conf("gtnet-skt-initial-values", is_list=True, convert=lambda x: x.lower())  # type: List[str]

        # Elastic-config
        self.elastic_enabled = True if self._conf("elastic-enabled").lower() == "true" else False  # type: bool
        self.elastic_host = self._conf("elastic-host")  # type: str

        # Validate configuration values
        if self.retry_delay <= 0.0:
            raise ValueError(f"'rtds-retry-delay' must be a positive float, not {self.retry_delay}")
        if self.rack_ip.count(".") != 3:
            raise ValueError(f"invalid IP for 'rtds-rack-ip': {self.rack_ip}")
        if self.csv_rows_per_file <= 0:
            raise ValueError(f"'csv-rows-per-file' must be a positive integer, not {self.csv_rows_per_file}")
        if not self.pmu_ips or any(x.count(".") != 3 for x in self.pmu_ips):
            raise ValueError(f"invalid value(s) for 'rtds-pmu-ips': {self.pmu_ips}")
        if not (len(self.pmu_ips) == len(self.pmu_names) == len(self.pmu_ports) == len(self.pmu_labels) == len(self.pdc_ids)):
            raise ValueError("lengths of pmu configuration options don't match, are you missing a pmu in one of the options?")

        # Validate GTNET-SKT configuration values only if there are values configured
        if self.gtnet_skt_tag_types:
            if self.gtnet_skt_ip.count(".") != 3:
                raise ValueError(f"invalid IP for 'gtnet-skt-ip': {self.gtnet_skt_ip}")
            if self.gtnet_skt_protocol not in ["tcp", "udp"]:
                raise ValueError(f"invalid protocol '{self.gtnet_skt_protocol}' for 'gtnet_skt_protocol', must be 'tcp' or 'udp'")
            if len(self.gtnet_skt_tag_names) != len(self.gtnet_skt_tag_types):
                raise ValueError(f"length of 'gtnet-skt-tag-names' doesn't match length of 'gtnet-skt-tag-types'")
            if len(self.gtnet_skt_initial_values) != len(self.gtnet_skt_tag_types):
                raise ValueError(f"length of 'gtnet-skt-initial-values' doesn't match length of 'gtnet-skt-tag-types'")
            if any(t not in ["int", "float"] for t in self.gtnet_skt_tag_types):
                raise ValueError(f"invalid type present in 'gtnet-skt-tag-types', only 'int' or 'float' are allowed")
            if len(self.gtnet_skt_tag_names) > 30:  # max of 30 data points per channel, 10 channels total for GTNET card
                raise ValueError(f"maximum of 30 points allowed per GTNET-SKT channel, {len(self.gtnet_skt_tag_names)} are defined in config")

        # Configure logging to stdout (includes debug messages from pypmu)
        logging.basicConfig(
            level=logging.DEBUG if self.debug else logging.INFO,
            format="%(asctime)s.%(msecs)03d [%(levelname)s] (%(name)s) %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            stream=sys.stdout
        )
        self.log = logging.getLogger(self.__class__.__name__)
        self.log.setLevel(logging.DEBUG if self.debug else logging.INFO)
        self.log.info(f"Debug: {self.debug}")
        if not self.csv_enabled:
            self.log.warning("CSV output is DISABLED (since the 'csv-enabled' option is False)")
        elif not self.csv_max_files:
            self.log.warning("No limit set in 'csv-max-files', CSV files won't be cleaned up and could fill all available disk space")

        # Elasticsearch setup
        self.elastic_buffer = []  # buffer for data to push to elastic
        if self.elastic_enabled:
            logging.getLogger("elastic_transport").setLevel(logging.WARNING)
            logging.getLogger("elasticsearch").setLevel(logging.WARNING)
            self.log.info(f"Connecting to Elasticsearch host {self.elastic_host}")
            self.__es = Elasticsearch(self.elastic_host)
            es_info = self.__es.info()  # cause connection to be created
            self.log.info(f"Elasticsearch server info: {es_info}")
        else:
            self.log.warning("Elasticsearch output is DISABLED (since the 'elastic-enabled' option is False)")
            self.__es = None

        # Create PMU instances: IP, Port, Name, Label, PDC ID
        self.pmus = []
        pmu_info = zip(self.pmu_ips, self.pmu_names, self.pmu_ports, self.pmu_labels, self.pdc_ids)
        polling_active = False
        while not polling_active:
            try:
                for ip, name, port, label, pdc_id in pmu_info:
                    pmu = PMU(ip=ip, port=port, pdc_id=pdc_id, name=name, label=label)
                    pmu.run()
                    self.pmus.append(pmu)
            except Exception as ex:
                self.log.error(f"Failed to connect to PMUs: {ex}\nSleeping for {self.retry_delay} seconds before retrying connection")
                if self.debug:
                    self.log.error(f"traceback for connection failure: {traceback.format_exc()}")
                sleep(self.retry_delay)
            else:
                polling_active = True
        self.log.info(f"Instantiated and started {len(self.pmus)} PMUs")

        # Current values, keyed by tag name string
        self.current_values = {}  # type: Dict[str, Any]

        # Socket to be used for writes. This avoids opening/closing a TCP connection on every write
        self.__gtnet_socket = None  # type: Optional[socket.socket]

        self.gtnet_skt_tags = {name: typ for name, typ in zip(self.gtnet_skt_tag_names, self.gtnet_skt_tag_types)}  # type: Dict[str, str]
        self.log.debug(f"gtnet_skt_tags: {self.gtnet_skt_tags}")

        # Tracks current state of values for all GTNET-SKT points
        # The state is initialized using 'gtnet-skt-initial-values' from provider config
        # Refer to docstring for RTDS.write() for details
        # Also, pre-generate the struct format, since it won't change
        self.struct_format_string = "!"  # '!' => network byte order
        self.gtnet_skt_state = {}  # type: Dict[str, int | float]

        for name, typ, val in zip(self.gtnet_skt_tag_names, self.gtnet_skt_tag_types, self.gtnet_skt_initial_values):
            if typ == "int":
                self.gtnet_skt_state[name] = int(val)
                self.struct_format_string += "i"
            elif typ == "float":
                self.gtnet_skt_state[name] = float(val)
                self.struct_format_string += "f"
            else:
                raise ValueError(f"invalid type {typ} for GTNET-SKT tag {name} with initial value {val}")

        # Allow gtnet-skt fields to be read
        self.current_values.update(self.gtnet_skt_state)

        # Begin polling PMUs
        self.__pmu_thread = threading.Thread(target=self._start_poll_pmus)
        self.__pmu_thread.start()

        # Start Elasticsearch pusher thread
        if self.elastic_enabled:
            logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)
            self.__es_thread = threading.Thread(target=self._elastic_pusher)
            self.__es_thread.start()

        self.log.info("RTDS initialization is finished")

    def _conf(self, key: str, is_list: bool = False, convert=None) -> Any:
        """Read a value out of the configuration file section for the service."""
        val = self.config.get("power-solver-service", key)

        if isinstance(val, str):
            val = val.strip()

        if is_list:
            if not isinstance(val, str):
                raise ValueError(f"expected comma-separated list for '{key}'")
            val = [x.strip() for x in val.split(",") if x.strip()]
            if convert:
                val = [convert(x) for x in val]

        return val

    def _start_poll_pmus(self):
        """Query for data from the PMUs via C37.118 as fast as possible."""
        self.log.info(f"Sending start request to {len(self.pmus)} PMUs...")
        for pmu in self.pmus:
            pmu.sequence = 0
            pmu.start()  # Request to start sending measurements

        self.log.info(f"Starting polling threads for {len(self.pmus)} PMUs...")
        threads = []
        for pmu in self.pmus:
            pmu_thread = threading.Thread(target=self._poll_pmu, args=(pmu,))
            pmu_thread.start()
            threads.append(pmu_thread)

        # Save PMU metadata (configs and/or headers) to file
        metadata = {}
        for pmu in self.pmus:
            # Wait until configs have been pulled to save them
            while not self.pmus[0].pmu_config:
                sleep(1)
            metadata[str(pmu)] = {"config": pmu.pmu_config.__dict__}
            if pmu.pmu_header:
                metadata[str(pmu)]["header"] = pmu.pmu_header.__dict__

        timestamp = datetime.utcnow().strftime("%d-%m-%Y_%H-%M-%S")

        meta_path = Path(self.csv_path, f"pmu_metadata_{timestamp}.json")
        if not meta_path.parent.exists():
            meta_path.parent.mkdir(exist_ok=True, parents=True)
        self.log.info(f"Writing metadata from {len(metadata)} PMUs to {meta_path}")
        meta_path.write_text(json.dumps(metadata, indent=4), encoding="utf-8")

        # Wait for values to be read from PMUs
        while not self.current_values or self.current_values == self.gtnet_skt_state:
            sleep(1)

        # Save the tag names to a file after there are values from the PMUs
        tags_path = Path(self.csv_path, f"tags_{timestamp}.txt")
        tags = list(self.current_values.keys())
        self.log.info(f"Writing {len(tags)} tag names to {tags_path}")
        tags_path.write_text("\n".join(tags), encoding="utf-8")

        # Block on PMU threads
        for thread in threads:
            thread.join()

    def _rebuild_pmu_connection(self, pmu: PMU):
        """
        Rebuild TCP connection to PMU if connection fails.

        For example, if RTDS simulation is stopped, the PMUs no longer exist.
        When the simulation restarts, this provider should automatically reconnect
        to the PMUs and start getting data again.
        """
        if pmu.pmu.pmu_socket:
            pmu.pmu.quit()

        successful = False
        while not successful:
            try:
                pmu.pmu.run()  # attempt to connect
                pmu.run()  # re-initialize
                pmu.sequence = 0  # reset sequence number
                successful = True
            except Exception as ex:
                self.log.error(f"Failed to rebuild PMU connection to {str(pmu)} due to error '{ex}', sleeping for {self.retry_delay} seconds before attempting again...")
                if pmu.pmu.pmu_socket:
                    pmu.pmu.quit()
                sleep(self.retry_delay)

    def _poll_pmu(self, pmu: PMU):
        """
        Continually polls for data from a PMU and updates ``self.current_values``.

        NOTE: This method is intended to be run in a thread,
        since it's loop that runs forever until killed.
        """
        self.log.info(f"Started polling thread for {str(pmu)}")
        retry_count = 0

        while True:
            try:
                data_frame = pmu.get_data_frame()
            except Exception as ex:
                self.log.error(f"Failed to get data frame from {str(pmu)} due to an exception '{ex}', attempting to rebuild connection...")
                if self.debug:  # only log traceback if debugging
                    self.log.exception(f"traceback for {str(pmu)}")
                self._rebuild_pmu_connection(pmu)
                continue

            if not data_frame:
                if retry_count >= 3:
                    self.log.error(f"Failed to request data {retry_count} times from {str(pmu)}, attempting to rebuild connection...")
                    self._rebuild_pmu_connection(pmu)
                    retry_count = 0
                else:
                    retry_count += 1
                    self.log.error(f"No data in frame from {str(pmu)}, sleeping for {self.retry_delay} seconds before retrying (retry count: {retry_count})")
                    sleep(self.retry_delay)
                continue

            ts_now = datetime.utcnow()

            for mm in data_frame["measurements"]:
                if mm["stat"] != "ok":
                    pmu.log.error(f"Bad/unknown PMU status: {mm['stat']}")
                if mm["stream_id"] != pmu.pdc_id:
                    pmu.log.warning(f"Unknown PMU stream ID: {mm['stream_id']} (expected {pmu.pdc_id})")

                # TODO: support PMU "digital" fields (mm["digital"])
                line = {
                    "sequence": pmu.sequence,  # int
                    "stream_id": mm["stream_id"],  # int
                    "time": data_frame["time"],  # float
                    "freq": mm["frequency"],  # float
                    "dfreq": mm["rocof"],  # float
                    "phasors": {  # dict of dict, keyed by phasor ID (int)
                        # tuple of floats, (real, angle)
                        i: {"real": ph[0], "angle": ph[1]}
                        for i, ph in enumerate(mm["phasors"])
                    },
                }

                # Create CSV writer if it doesn't exist
                if self.csv_enabled and pmu.csv_writer is None:
                    header = ["sequence", "rtds_time", "sceptre_time", "freq", "dfreq"]
                    for i, ph in line["phasors"].items():
                        for k in ph.keys():
                            # Example: PHASOR CH 1:VA_real
                            header.append(f"{pmu.channel_names[i]}_{k}")
                    pmu.csv_writer = RotatingCSVWriter(
                        name=str(pmu),
                        csv_dir=self.csv_path / str(pmu),
                        header=header,
                        filename_base=f"{str(pmu)}",
                        rows_per_file=self.csv_rows_per_file,
                        max_files=self.csv_max_files
                    )

                # TODO: move CSV writing into threads like Elastic is?
                # Write data to CSV file
                if self.csv_enabled:
                    csv_row = [line["sequence"], line["time"], ts_now.timestamp(), line["freq"], line["dfreq"]]
                    for ph in line["phasors"].values():
                        for v in ph.values():
                            csv_row.append(v)
                    pmu.csv_writer.write(csv_row)

                # Save data to Elasticsearch
                if self.elastic_enabled:
                    rtds_time = datetime.utcfromtimestamp(data_frame["time"])
                    es_bodies = []
                    for ph_id, phasor in line["phasors"].items():
                        es_body = {
                            "@timestamp": rtds_time,
                            "rtds_time": rtds_time,
                            "sceptre_time": ts_now,
                            "pmu": {
                                "name": pmu.name,
                                "label": pmu.label,
                                "ip": pmu.ip,
                                "port": pmu.port,
                                "id": pmu.pdc_id,
                            },
                            "measurement": {
                                "stream": mm["stream_id"],  # int
                                "status": mm["stat"],  # str
                                "sequence": pmu.sequence,  # int
                                "frequency": mm["frequency"],  # float
                                "dfreq": mm["rocof"],  # float
                                "channel": pmu.channel_names[ph_id],  # str
                                "phasor": {
                                    "id": ph_id,  # int
                                    "real": phasor["real"],  # float
                                    "angle": phasor["angle"],  # float
                                },
                            },
                        }
                        es_bodies.append(es_body)
                    with self.__es_lock:
                        self.elastic_buffer.extend(es_bodies)

                # Update global data structure with measurements
                #
                # NOTE: since there are usually multiple threads querying from multiple PMUs
                # simultaneously, a lock mutex is used to ensure self.current_values doesn't
                # result in a race condition or corrupted data.
                with self.__lock:
                    for ph_id, ph in line["phasors"].items():
                        # two types: "real", "angle"
                        for ph_type, val in ph.items():
                            # Example: BUS6_VA.real
                            tag = f"{pmu.label}_{pmu.channel_names[ph_id]}.{ph_type}"
                            self.current_values[tag] = val

                    # TODO: better handling of analog values, this is a hack for the HARMONIE LDRD
                    if mm["analog"]:
                        for i, analog_value in enumerate(mm["analog"]):
                            self.current_values[f"{pmu.name}_ANALOG_{i+1}.real"] = analog_value
                            self.current_values[f"{pmu.name}_ANALOG_{i+1}.angle"] = analog_value
                pmu.sequence += 1

    def _elastic_pusher(self):
        self.log.info("Starting Elasticsearch pusher thread")
        if not self.__es:
            raise RuntimeError("self.__es not defined")

        # Only need to create this dict once
        es_additions = {
            "event": {},
            "ecs": {
                "version": "8.1.0"
            },
            "agent": {
                "type": "rtds-sceptre-provider",
                "version": __version__
            },
            "observer": {
                "hostname": platform.node(),
                "geo": {
                    "timezone": str(datetime.now(timezone.utc).astimezone().tzinfo)
                }
            },
            "network": {
                "protocol": "c37.118",
                "transport": "tcp",
            },
        }

        while True:
            sleep(0.1)  # check every 50ms to prevent eating up CPU just spinning
            if self.elastic_buffer:
                with self.__es_lock:
                    messages = list(self.elastic_buffer)
                    self.elastic_buffer = []

                # TODO: push pre-defined type mapping when creating index
                ts_now = datetime.now()
                index = f"rtds-{ts_now.strftime('%Y.%m.%d')}"
                es_additions["event"]["ingested"] = ts_now

                actions = [  # type: List[dict]
                    {
                        "_index": index,
                        "_source": {**es_additions, **message}
                    }
                    for message in messages
                ]

                try:
                    result = helpers.bulk(self.__es, actions, request_timeout=30)
                    if self.debug and not result:
                        self.log.error(f"Empty ES bulk result: {result}")
                except Exception:
                    self.log.exception("failed ES bulk push")

    def _serialize_value(self, tag: str, value: Any) -> str:
        """
        Convert a value to to a valid string for sending to subscribers (field devices).
        """
        if isinstance(value, bool):
            if value is True:
                return "true"
            else:
                return "false"

        if tag in self.gtnet_skt_tags and self.gtnet_skt_tags[tag] == "int":
            if int(value) >= 1:
                return "true"
            else:
                return "false"

        return str(value)

    def query(self) -> str:
        """
        Return all current tag names.

        Tag format for PMU data:
            {pmu-label}_{channel}.real
            {pmu-label}_{channel}.angle

        Tag examples for PMU data:
            BUS6_VA.real
            BUS6_VA.angle
        """
        self.log.debug("Processing query request")

        if not self.current_values:
            msg = "ERR=No data points have been read yet from the RTDS"
        else:
            msg = f"ACK={','.join(self.current_values.keys())}"

        self.log.log(  # Log at DEBUG level, unless there's an error
            logging.ERROR if "ERR" in msg else logging.DEBUG,
            f"Query response: {msg}"
        )

        return msg

    def read(self, tag: str) -> str:
        self.log.debug(f"Processing read request for tag '{tag}'")

        if not self.current_values:
            msg = "ERR=Data points have not been initialized yet from the RTDS"
        elif tag not in self.current_values:
            msg = "ERR=Tag not found in current values from RTDS"
        else:
            msg = f"ACK={self._serialize_value(tag, self.current_values[tag])}"

        self.log.log(  # Log at DEBUG level, unless there's an error
            logging.ERROR if "ERR" in msg else logging.DEBUG,
            f"Read response for tag '{tag}': {msg}"
        )

        return msg

    def write(self, tags: dict) -> str:
        self.log.debug(f"Processing write request for tags: {tags}")

        if not tags:
            msg = "ERR=No tags provided for write to RTDS"
            self.log.error(msg)
            return msg

        # In OPC, there will be 3 tags for a point named "G1CB1":
        # - G1CB1_binary_output_closed
        # - G1CB1_binary_output_closed_opset
        # - G1CB1_binary_output_closed_optype
        #
        # To write to a point: set "G1CB1_binary_output_closed" to "1" in Quick Client in TOPServer in OPC
        # This will write "G1CB1.closed" to SCEPTRE.
        # To read status of a point from OPC, read "G1CB1_binary_output_closed_opset".
        # This is because DNP3 can't have values that are written and read, apparently.
        # So, G1CB1_binary_output_closed is "write", G1CB1_binary_output_closed_opset is "read".
        # The quality of "G1CB1_binary_output_closed" will show up as "Bad" in QuickClient, since it has no value.

        # Validate all incoming tag names match what's in config
        for tag, val in tags.items():
            if tag not in self.gtnet_skt_tags:
                msg = f"ERR=Invalid tag name '{tag}' for write to RTDS"
                self.log.error(f"{msg} (tags being written: {tags})")
                return msg

        # Update the state tracker with the values from the tags being written
        for tag, val in tags.items():
            # Validate types match what's in config, if they don't, then warn and typecast
            # NOTE: these will be strings if coming from pybennu-probe
            if not isinstance(val, str) and type(val).__name__ != self.gtnet_skt_tags[tag]:
                self.log.warning(
                    f"Data type of tag '{tag}' is '{type(val).__name__}', not "
                    f"'{self.gtnet_skt_tags[tag]}', forcing a typecast. If "
                    f"you're using 'pybennu-probe', this is normal."
                )

            if val is False or (isinstance(val, str) and val.lower() == "false"):
                val = "0"
            elif val is True or (isinstance(val, str) and val.lower() == "true"):
                val = "1"

            if self.gtnet_skt_tags[tag] == "int":
                typecasted_value = int(val)
            else:
                typecasted_value = float(val)

            # NOTE: don't need to sort incoming values, since they're updating the
            # dict (gtnet_skt_state), which is already in the proper order.
            self.log.info(f"Updating GTNET-SKT tag '{tag}' to {typecasted_value} "
                          f"(previous value: {self.gtnet_skt_state[tag]})")
            self.gtnet_skt_state[tag] = typecasted_value

        # Generate the payload bytes to be sent across the socket
        # NOTE: see docstring at top of this file for details on GTNET-SKT protocol
        values = list(self.gtnet_skt_state.values())
        payload = struct.pack(self.struct_format_string, *values)  # type: bytes
        self.log.debug(f"Raw payload for {len(tags)} tags: {payload}")

        if not self.__gtnet_socket:
            self._init_gtnet_socket()

        sent = False
        while not sent:
            try:
                # Send the payload via TCP or UDP. No special structure or
                # tagging, this is a very basic protocol :)
                if self.gtnet_skt_protocol == "tcp":
                    self.__gtnet_socket.send(payload)
                else:  # UDP
                    self.__gtnet_socket.sendto(payload, (self.gtnet_skt_ip, self.gtnet_skt_port))
                sent = True
            except Exception:
                self.log.exception(f"GTNET-SKT send failed, resetting connection...")
                self._init_gtnet_socket()

        # Update current values so GTNET-SKT points can be read from in addition to written
        with self.__lock:
            self.current_values.update(self.gtnet_skt_state)

        msg = f"ACK=Wrote {len(tags)} tags to RTDS via GTNET-SKT"
        self.log.debug(msg)

        return msg

    def _init_gtnet_socket(self):
        """
        Initialize TCP or UDP socket, and if TCP connection fails, retry until it's successful.
        """
        self._reset_gtnet_socket()

        if self.gtnet_skt_protocol == "tcp":
            target = (self.gtnet_skt_ip, self.gtnet_skt_port)
            connected = False
            while not connected:  # loop to handle connection failures
                try:
                    self.__gtnet_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    self.__gtnet_socket.connect(target)
                    connected = True
                except Exception:
                    self.log.error(f"Failed to connect to GTNET-SKT {target}, sleeping for {self.retry_delay} seconds before retrying")
                    if self.debug:  # only log traceback if debugging
                        self.log.exception(f"traceback for GTNET-SKT {target}")
                    self._reset_gtnet_socket()
                    sleep(self.retry_delay)
        else:  # UDP
            self.__gtnet_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _reset_gtnet_socket(self):
        if self.__gtnet_socket:
            try:
                self.__gtnet_socket.close()
            except Exception:
                pass
            self.__gtnet_socket = None

    def periodic_publish(self):
        """
        Publish all tags periodically.

        Publish rate is configured by the 'publish-rate' configuration option.
        If publish rate is 0, then points will be published as fast as possible.

        Publisher message format:
            WRITE=<tag name>:<value>[,<tag name>:<value>,...]
            WRITE={tag name:value,tag name:value}
        """
        self.log.info(f"Beginning periodic publish (publish rate: {self.publish_rate})")
        while True:
            with self.__lock:  # mutex to prevent threads from writing while we're reading
                tags = [
                    f"{tag}:{self._serialize_value(tag, value)}"
                    for tag, value in self.current_values.items()
                ]

            msg = "Write={" + ",".join(tags) + "}"
            self.publish(msg)

            # If publish_rate is not positive (0), don't sleep and go as fast as possible
            # Otherwise (if it's non-zero), log the points, and sleep like usual
            if self.publish_rate:
                # NOTE: to get the raw values, run 'pybennu-test-subscriber' on any RTU
                # if self.debug:
                #     self.log.debug(f"Published {len(tags)} points (publish rate: {self.publish_rate})")
                #     self.log.debug(f"msg: {msg}")
                sleep(self.publish_rate)
