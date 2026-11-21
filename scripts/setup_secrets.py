"""Create local configuration without displaying secrets or overwriting existing .env."""

from pathlib import Path
import secrets, os

p = Path(__file__).resolve().parents[1] / ".env"
if p.exists():
    raise SystemExit(".env exists; edit it locally to configure OPENROUTER_API_KEY.")
text = (
    p.with_name(".env.example")
    .read_text()
    .replace("OPERATOR_TOKEN=", "OPERATOR_TOKEN=" + secrets.token_urlsafe(32))
    .replace(
        "GRAFANA_ADMIN_PASSWORD=", "GRAFANA_ADMIN_PASSWORD=" + secrets.token_urlsafe(24)
    )
)
text = text.replace("LOCAL_UID=10001", "LOCAL_UID=" + str(os.getuid())).replace(
    "LOCAL_GID=10001", "LOCAL_GID=" + str(os.getgid())
)
(p.parent / "storage").mkdir(exist_ok=True, mode=0o700)
fd = p.open("x")
fd.write(text)
fd.close()
p.chmod(0o600)
print(
    "Created restricted .env. Set OPENROUTER_API_KEY locally; values were not printed."
)
