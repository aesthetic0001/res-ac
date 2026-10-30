# Eduroam device tools

The Seeeduino Cloud uses its Linux processor for Wi-Fi. Enterprise eduroam
connections require the full `wpad` package and an institution-specific profile.
Use your institution's current IT documentation for the EAP method, inner
method, CA certificate, expected server name, and anonymous outer identity.
No institution, account, network address, or deployed Wi-Fi profile is bundled
in this repository.

The helpers assume a prepared UCI section named `wireless.eduroam` and a DHCP
interface named `wwan`. The configuration must validate the authentication
server's certificate and server name. Keep the board's clock correct so that
certificate validity checks work. Some old firmware/radio combinations work
more reliably in client-only mode than when also running an access point.

Close other serial monitors and enter credentials from the host:

```sh
~/.platformio/penv/bin/python scripts/eduroam_credentials.py
```

Enter the full identity supplied by your institution, or provide its realm with
`--realm`. Password input is hidden and goes to a receiver process rather than
shell history. `--check` uses dummy values in an isolated temporary UCI profile.
The script requires PySerial, available in a typical PlatformIO environment.

On the board's Linux serial console:

```sh
python /root/check-eduroam.py
python /root/check-eduroam.py --connect
```

The checker reports stored credential presence and authentication/DHCP status.
Passwords stay hidden unless `--show-credentials` is explicitly requested; never
paste or commit the output of that option. Generic EAP errors and timeouts do
not establish that a password is wrong.

The host helper expects `scripts/eduroam_receiver.py` to be installed as
`/root/eduroam-receiver.py`, and the checker as `/root/check-eduroam.py`. Transfer
scripts through a trusted local connection and verify checksums, since long
commands can be corrupted by old serial bridges. The receiver supports the
board's Python 2.7 without relying on its incomplete JSON module.

Device passwords and the actual UCI profile belong only on the board, in
`/etc/config/wireless` with mode 600. Keep network backups and local diagnosis
notes in the ignored `private/` directory. No board configuration dump, device
backup, certificate, diagnostic log, or deployment secret belongs in Git.
