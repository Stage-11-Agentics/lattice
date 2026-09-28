# Setup for running docs/hosted/api.md through scripts/run_hosted_guide.py:
# the guide's quick start, reduced to a server, project demo, and a saved token.
mkdir -p "$HOME/lattice-trial" && cd "$HOME/lattice-trial"
export TRIAL="$PWD"
export LATTICE_SERVER_ROOT="$TRIAL/server-root"
lattice server init > /dev/null
lattice server project create demo --code DEMO > /dev/null
umask 077
lattice server token create --user human:alice --machine laptop --project demo | head -n 1 > "$TRIAL/token"
python3 - "$LATTICE_SERVER_ROOT" "$TRIAL" <<'PY'
import subprocess, sys
root, trial = sys.argv[1], sys.argv[2]
with open(f"{trial}/server.log", "ab") as log:
    server = subprocess.Popen(
        ["lattice", "server", "serve", "--root", root],
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
with open(f"{trial}/server.pid", "w") as pid:
    pid.write(f"{server.pid}\n")
PY
for i in $(seq 1 50); do
  curl -sf http://127.0.0.1:8740/healthz > /dev/null && break
  sleep 0.2
done
