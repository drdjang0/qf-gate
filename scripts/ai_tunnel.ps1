# Keeps the SSH tunnel to the self-hosted LLM open (Colibri on the AI VPS, WireGuard 10.8.0.5:8000),
# reconnecting a few seconds after it drops. The app talks to it at http://127.0.0.1:18000/v1
# (INTEL_LLM_BASE_URL in .env). Uses the "ai-vps" host from ~/.ssh/config. Stop with Ctrl-C.
while ($true) {
    ssh -N -o ServerAliveInterval=20 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -L 18000:10.8.0.5:8000 ai-vps
    Write-Host "$(Get-Date -Format s) tunnel to ai-vps dropped; reconnecting in 2 s"
    Start-Sleep -Seconds 2
}
