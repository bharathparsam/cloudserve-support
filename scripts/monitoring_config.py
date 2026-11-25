"""Prepare native monitoring configs from the same sources used by Compose."""
from pathlib import Path


def prepare(root):
    root = Path(root).resolve()
    storage = root / 'storage'
    storage.mkdir(exist_ok=True)
    prom = (root / 'monitoring/prometheus.yml').read_text()
    prom = prom.replace('host.docker.internal', '127.0.0.1').replace('/etc/prometheus/rules', str(root / 'monitoring/rules'))
    (storage / 'prometheus-local.yml').write_text(prom)
    for group in ('dashboards', 'datasources'):
        source = root / 'monitoring/grafana/provisioning' / group / 'default.yml'
        target = storage / 'grafana-provisioning' / group / 'default.yml'
        target.parent.mkdir(parents=True, exist_ok=True)
        text = source.read_text().replace('http://prometheus:9090', 'http://127.0.0.1:9090').replace('/var/lib/grafana/dashboards', str(root / 'monitoring/grafana/dashboards'))
        target.write_text(text)
    return storage


if __name__ == '__main__':
    storage = prepare(Path(__file__).resolve().parents[1])
    print('Prepared native Prometheus and Grafana configuration in', storage)
    print('Follow docs/OPERATIONS_GUIDE.md for service start commands.')
