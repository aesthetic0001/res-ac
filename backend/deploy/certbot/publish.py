"""Certbot deploy hook: publish a readable copy for Node, leaving ACME state private."""
import os
import shutil
import tempfile

source = os.environ.get('RENEWED_LINEAGE', '/etc/letsencrypt/live/sensor-backend')
destination = '/run/sensor-tls'
os.makedirs(destination, mode=0o755, exist_ok=True)
for name in ('fullchain.pem', 'privkey.pem'):
    fd, temporary = tempfile.mkstemp(prefix='.' + name, dir=destination)
    try:
        with os.fdopen(fd, 'wb') as output, open(os.path.join(source, name), 'rb') as incoming:
            shutil.copyfileobj(incoming, output)
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary, 1000, 1000)
        os.chmod(temporary, 0o600)
        os.replace(temporary, os.path.join(destination, name))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
print('Published renewed certificate for the Node HTTPS server.')
