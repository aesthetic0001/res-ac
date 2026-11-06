#!/bin/sh
# Invoked by Bridge.begin on the UART console; stdout is protocol bytes only.
umask 077
gzip -dc /usr/lib/res-ac/bridge.gz > /tmp/res-ac-bridge.new || exit 1
chmod 700 /tmp/res-ac-bridge.new || exit 1
/tmp/res-ac-bridge.new --version | grep -q '^res-ac-bridge protocol=1 uart=yun1$' || exit 1
mv /tmp/res-ac-bridge.new /tmp/res-ac-bridge || exit 1
exec /tmp/res-ac-bridge 2>/tmp/res-ac-bridge.log
