# Install

A lab needs one nest and as many rats as it has benches. A single bench can also run a rat alone, or a nest
with its devices plugged straight into it.

## Choosing hardware and an operating system

| Role | Hardware | Operating system |
| --- | --- | --- |
| Nest | any 64-bit Linux computer: a small server, a mini PC or a Raspberry Pi 5 with 4 GB or more | Ubuntu Server LTS or Debian stable, or Raspberry Pi OS Lite (64-bit) on a Pi |
| Rat for GPIO, serial and USB instruments | Raspberry Pi Zero 2 W, Pi 4 or Pi 5, 1 GB is plenty | Raspberry Pi OS Lite (64-bit) |
| Rat for cameras or an HDMI SLM | Raspberry Pi 5 with 2 GB or more | Raspberry Pi OS Lite (64-bit) |
| Rat on another board | any board with a mainline kernel | Armbian, Debian or Ubuntu |

Use the Lite or server images, without a desktop. A rat with no desktop leaves the HDMI outputs free for an
SLM or DMD, boots faster and has less to go wrong. LABRAT needs Python 3.11 or newer: Debian 12 or later,
Ubuntu 24.04 LTS or later, and Raspberry Pi OS based on Debian 12 or later all have it.

Some advice from running rats at benches:

- Wire rats to Ethernet where you can. Wi-Fi works, but drops are the most common cause of a rat going
  offline. Power over Ethernet with a PoE HAT gives power and network in one cable.
- Use the official power supply. An undersized one browns the board out when a USB instrument draws current.
- Use a good SD card, or boot from USB or NVMe on a Pi 5. Cheap cards wear out and corrupt on power cuts.
- Set the rat's host name, Wi-Fi and SSH key in Raspberry Pi Imager before the first boot.

## The nest

On the nest computer, from a copy of the repository or a release archive:

```bash
sudo ./packaging/install.sh nest
```

The script

1. creates a system user `labrat`,
2. installs LABRAT into `/opt/labrat` in its own virtual environment,
3. writes `/etc/labrat/nest.toml` with a first admin token, which it prints once,
4. installs and starts the `labrat-nest` systemd service.

Keep the printed token: it is shown only once and only its hash is stored. Make more tokens with
`labrat token --name <who> --role <viewer|operator|admin>` and add the lines it prints to `nest.toml`.

Check it:

```bash
systemctl status labrat-nest
curl -s http://localhost:8780/api/v1/health
```

## A rat

On each rat:

```bash
sudo ./packaging/install.sh rat
```

The script does the same as for the nest, with three differences. It adds the `labrat` user to the groups
that own serial ports, GPIO and video (`dialout`, `gpio`, `video`, `plugdev`, where they exist). It installs
the serial and GPIO extras and lets the virtual environment see packages installed with apt, such as Aravis.
It writes `/etc/labrat/rat.toml` with a simulated supply, so the rat serves something at once, and a commented
example for every serial port and USBTMC instrument it finds and for the first GPIO chip. Edit that file to
describe the devices, remove the simulated supply, then `sudo systemctl restart labrat-rat`.

Show the rat's key:

```bash
sudo labrat rat key --config /etc/labrat/rat.toml
```

## Adopting a rat

From any computer with an admin token for the nest:

```bash
export LABRAT_URL=http://<nest>:8780 LABRAT_TOKEN=<admin token>
labrat find                              # nests and rats on this network, with key fingerprints
labrat adopt bench-rat                   # asks for the key, compare the fingerprint with 'labrat rat key' 
labrat devices                           # the rat's devices now appear as bench-rat/<device>
```

When multicast DNS does not cross your network, give the address: `labrat adopt bench-rat --at
http://<rat>:8781`. Scripts can pass the key in the environment variable `LABRAT_RAT_KEY`.

## By hand

```bash
pipx install 'labrat-lab[rat]'           # or plain labrat-lab on a nest
labrat nest init                         # or: labrat rat init
labrat nest                              # or: labrat rat
labrat service nest                      # prints a systemd unit to adapt
```

The printed unit runs as the `labrat` user, reads `/etc/labrat/<kind>.toml` and blocks access to home
directories, so it suits the layout the install script makes. For an installation in a home directory, adjust
`User`, `ExecStart` and `ProtectHome` before using it.

Optional pieces:

| For | Install |
| --- | --- |
| serial instruments | `pip install 'labrat-lab[serial]'` |
| GPIO | `pip install 'labrat-lab[gpio]'` |
| cameras | `sudo apt install ffmpeg` |
| GigE Vision and USB3 Vision cameras | `sudo apt install gir1.2-aravis-0.8 python3-gi`, and create the virtual environment with `--system-site-packages` |
| PNG frames for the display driver | `pip install pillow` |

## Updating

```bash
sudo /opt/labrat/venv/bin/pip install --upgrade labrat-lab
sudo systemctl restart labrat-nest       # or labrat-rat
```

A service that stops puts its own devices in their safe state first. A nest that restarts leaves its rats'
devices as they are.

## Removing

```bash
sudo systemctl disable --now labrat-nest labrat-rat
sudo rm -rf /opt/labrat /etc/systemd/system/labrat-*.service
```

`/etc/labrat` (configuration) and `/var/lib/labrat` (keys, adopted rats and the audit log) are left for you to
keep or delete.
