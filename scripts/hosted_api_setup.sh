# Setup for running docs/hosted/api.md through scripts/run_hosted_guide.py:
# the guide's quick start, reduced to a server, project demo, and a saved token.
mkdir -p "$HOME/lattice-trial" && cd "$HOME/lattice-trial"
export TRIAL="$PWD"
export LATTICE_SERVER_ROOT="$TRIAL/server-root"
lattice server init > /dev/null
lattice server project create demo --code DEMO > /dev/null
umask 077
lattice server token create --user human:alice --machine laptop --project demo | head -n 1 > "$TRIAL/token"
nohup lattice server serve > "$TRIAL/server.log" 2>&1 &
echo $! > "$TRIAL/server.pid"
sleep 2
