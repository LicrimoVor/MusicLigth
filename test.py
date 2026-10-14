import logging
import tinytuya

logging.basicConfig(level=logging.DEBUG)

device = tinytuya.Device(
    dev_id="bfbd4cb669009901e7p2fh", address="192.168.1.38", local_key="c?h8*^2RsatunAiC"
)

device.set_version(3.5)
device.set_socketTimeout(5)
device.set_socketRetryLimit(1)

try:
    print(device.status())
finally:
    device.close()
