import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ISS element set from the sgp4 library documentation (epoch 2019-12-09).
ISS_L1 = "1 25544U 98067A   19343.69339541  .00001764  00000-0  38792-4 0  9991"
ISS_L2 = "2 25544  51.6439 211.2001 0007417  17.6667  85.6398 15.50103472202482"
T0 = 1575910800.0        # 2019-12-09 17:00:00 UTC

ISS_OMM = {
    "OBJECT_NAME": "ISS (ZARYA)", "OBJECT_ID": "1998-067A", "EPOCH": "2019-12-09T16:38:29.363424",
    "MEAN_MOTION": 15.50103472, "ECCENTRICITY": 0.0007417, "INCLINATION": 51.6439,
    "RA_OF_ASC_NODE": 211.2001, "ARG_OF_PERICENTER": 17.6667, "MEAN_ANOMALY": 85.6398,
    "EPHEMERIS_TYPE": 0, "CLASSIFICATION_TYPE": "U", "NORAD_CAT_ID": 25544, "ELEMENT_SET_NO": 999,
    "REV_AT_EPOCH": 20248, "BSTAR": 0.38792e-4, "MEAN_MOTION_DOT": 0.00001764, "MEAN_MOTION_DDOT": 0,
}


def omm(satnum, name="SAT", raan=211.2, ma=85.6, **over):
    row = dict(ISS_OMM, NORAD_CAT_ID=satnum, OBJECT_NAME=name, RA_OF_ASC_NODE=raan, MEAN_ANOMALY=ma)
    row.update(over)
    return row
