# A network of its own

A nest or a rat can run a private network for its instruments and rats, on a spare Ethernet port or on a Wi-Fi
card as an access point. Everything on it gets an address from the nest, instruments keep the same address
every time, and discovery works whatever the building's network allows. The computer stays on its usual
network through its other port, for people, remote access and updates.

```toml
[network]
interface = "wlan1"
ssid = "labrat-bench"

[network.reserve]
afg = "08:00:11:00:00:0a"
```

```bash
labrat network plan        # checks the computer and shows every change
labrat network up          # applies it, with sudo
labrat network             # what is on it now
labrat network down        # removes it
```

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `interface` | required | the port or Wi-Fi card that carries the network (`ip link` lists them) |
| `ssid` | none | the Wi-Fi name, which makes the interface an access point (without it the interface is Ethernet) |
| `band` | `"2.4"` | `"2.4"` or `"5"` GHz (ESP32 boards need 2.4) |
| `channel` | chosen by the card | a Wi-Fi channel number |
| `address` | `"10.42.0.1/24"` | this computer's address on the network, with the network's size |
| `internet` | `false` | whether the devices on the network can reach beyond this computer |
| `[network.reserve]` | none | fixed addresses, by MAC address |

A reservation is `name = "mac"`, numbered from `.10` in the order written, or
`name = { mac = "...", address = "..." }` for a chosen address. A device's settings in LABRAT use the address,
such as `address = "tcp://10.42.0.10:5025"`. Other devices on the network can also reach it by its name.

## What `up` does

NetworkManager runs the network in its shared mode, which hands out addresses by DHCP and answers DNS. LABRAT
writes three files and starts the connection:

- `/etc/NetworkManager/system-connections/labrat-<name>.nmconnection`, the connection itself, readable by root
  only, as it holds the Wi-Fi password.
- `/etc/NetworkManager/dnsmasq-shared.d/labrat-<name>.conf`, the reservations.
- `/etc/NetworkManager/dispatcher.d/90-labrat-<name>`, with `internet = false`, a firewall rule (nftables) that
  drops traffic crossing this computer to or from the network. Devices on it reach the nest and each other,
  and nothing else. The outside reaches them only through the nest, with its tokens, limits and locks.

`labrat network plan` prints that script with the password hidden. `up` asks before it runs it, and `--yes`
skips the question. LABRAT itself keeps running as a normal user. `down` deletes the connection, the files and
the firewall rule.

The Wi-Fi network uses WPA2 with a 16-letter password made at the first `up` and kept in
`<state_dir>/network-password`, readable by its owner only. `labrat network --show-password` prints it, to set
up a device. A rat on an ESP32 takes the `ssid`, the password and the nest's address
(`labrat network` prints it) in its firmware settings.

## Checks

`plan` and `up` stop when

- NetworkManager is missing, or does not manage the interface,
- the interface does not exist, is the wrong kind for the settings, or cannot be an access point,
- the interface carries this computer's own connection,
- the address overlaps a network the computer is already on,
- `internet = false` and `nft` is missing.

`up --force` starts it anyway. Many built-in Wi-Fi cards cannot be a client and an access point at once. A USB
Wi-Fi adapter with AP mode, or a USB Ethernet adapter and a switch, gives the network a port of its own.

## Requirements

Linux with NetworkManager, which Raspberry Pi OS, Ubuntu and Debian desktops use, and nftables for a network
without internet. The settings are checked when the nest or rat starts, so a mistake in `[network]` shows
before anyone runs `up`.
