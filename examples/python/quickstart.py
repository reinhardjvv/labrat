"""Set a supply, measure it and make it safe, through a nest.

    labrat demo                              # in one terminal
    LABRAT_TOKEN=<token> python quickstart.py
"""
from labrat.client import Client
from labrat.message import LabratError

lab = Client()                               # LABRAT_URL (default http://127.0.0.1:8780) and LABRAT_TOKEN

for d in lab.devices():
    print(d["device"], d["kind"])

with lab.locked("psu"):
    lab.set("psu", "VoltageLevel", 5, channel="1")
    lab.set("psu", "CurrentLimit", 0.2, channel="1")
    lab.set("psu", "OutputEnabled", True, channel="1")
    print("current:", lab.call("psu", "Measure", channel="1", type="current"))
    try:
        lab.set("psu", "VoltageLevel", 50, channel="1")
    except LabratError as e:
        print("refused as expected:", e.code, e.message)

print(lab.stop("quickstart finished"))
