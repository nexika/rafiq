# Wake-on-LAN

"Rafiq, wake the gaming PC" sends a magic packet on the local network: a small UDP broadcast that a
sleeping or powered-off computer's network card listens for. Rafiq asks first, like any action.

It works over a cable (Ethernet). Most Wi-Fi cards ignore the packet when the computer is off.

## 1. Tell Rafiq about the computer

On the computer to wake, find its network card address:

```bash
ip link        # the "link/ether aa:bb:cc:dd:ee:ff" line under the wired card (eth0, enp3s0, ...)
```

Then on the computer running Rafiq, in `~/.config/rafiq/config.toml`:

```toml
[wake_on_lan.machines]
"gaming pc" = "aa:bb:cc:dd:ee:ff"
```

Say the name as it is written there; "wake the gaming PC" and "wake gaming" both find it.

If the packet does not arrive, set your network's own broadcast address instead of 255.255.255.255
(for 192.168.1.x it is 192.168.1.255), for all computers or for one:

```toml
[wake_on_lan]
broadcast = "192.168.1.255"

[wake_on_lan.machines]
nas = { mac = "11:22:33:44:55:66", broadcast = "192.168.2.255", port = 7 }
```

## 2. Let the computer be woken

**Firmware (BIOS/UEFI).** Turn on "Wake on LAN", "Power on by PCI-E" or "Resume by LAN". Turn off
"ErP" or "deep sleep" modes, which cut power to the network card.

**Linux.** Check and turn it on (replace `enp3s0` with the card's name from `ip link`):

```bash
sudo ethtool enp3s0 | grep Wake-on     # "Wake-on: g" means it is on; "d" means off
sudo ethtool -s enp3s0 wol g
```

That lasts until the next reboot. To keep it, with NetworkManager:

```bash
nmcli connection show                                   # find the wired connection's name
sudo nmcli connection modify "Wired connection 1" 802-3-ethernet.wake-on-lan magic
```

**Windows.** In Device Manager, the network card's properties: on Power Management tick "Allow this
device to wake the computer" and "Only allow a magic packet"; on Advanced enable "Wake on Magic
Packet". Turning off Fast Startup helps when the computer is fully shut down.

## 3. After a power cut

A computer that lost power usually stays off until its button is pressed. Set "Restore on AC power
loss" to "Power On" in the firmware, or plug it into a smart plug and switch the plug off and on.

## Try it

Shut the computer down, then say "Rafiq, wake the gaming PC" and answer yes. If nothing happens,
check that both computers are on the same network and that `ethtool` still shows `Wake-on: g`.
