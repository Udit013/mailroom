# Deploying a public demo

This guide runs the whole stack on one Oracle Cloud Always Free VM. The review page and API
share one HTTPS origin through Caddy.

**Status:** no real Oracle deployment has been tested. The Caddy routing, restart recovery,
and backup and restore procedures were rehearsed locally on an arm64 Docker host, over plain
HTTP. Real TLS, DuckDNS, Oracle's firewall and a real VM reboot have not been exercised.

**This is a demo, not production:**

- **Synthetic data only.** Files and extracted names are stored unencrypted. Never upload real
  documents.
- **It can disappear.** Oracle may reclaim idle Always Free resources, its free limits have
  changed before, and free capacity isn't guaranteed. Keep backups off the VM.
- **It runs on one host,** with no failover, monitoring or rate limiting.

Security assumptions are in [SECURITY.md](SECURITY.md). Check Oracle's current
[Always Free limits](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
before you start.

## Architecture

| Component | Runs as | Reachable from |
|---|---|---|
| Caddy | Host service | Internet, ports 80 and 443 |
| Review page | Static files in `/srv/mailroom` | Through Caddy |
| `api` | Compose | `127.0.0.1:8000`, through Caddy |
| `relay`, `worker`, `postgres`, `rabbitmq` | Compose | Docker network only |

Documents live on the `document-data` volume, which the API and worker share. That is why this
stack needs one host.

## 1. Create the VM

1. Create an Oracle Cloud account. Oracle asks for a mobile number and a card for verification.
   Choose your home region carefully, because Always Free compute must stay there.
2. Create an Always Free Arm instance, `VM.Standard.A1.Flex`, with Ubuntu and SSH key login. One
   OCPU and 3 GB of memory is enough. The stack idled at about 465 MiB when measured locally.
3. If you see "out of host capacity", try another availability domain or try again later.
4. Allow inbound TCP 22 from your IP address only, and 80 and 443 from anywhere, in the
   instance's network security rules.
5. Check the VM's own firewall too, because Oracle's images may block these ports as well.

Do not open 5432, 5672 or 8000.

## 2. Install the software

Follow the official guides for [Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
and [Caddy](https://caddyserver.com/docs/install). Then install `make`, let your user run Docker,
and start Docker at boot:

```bash
sudo apt-get install -y make
```

```bash
sudo usermod -aG docker "$USER"
```

```bash
sudo systemctl enable docker
```

Log out and back in. To stop container logs filling the disk, create `/etc/docker/daemon.json`,
then run `sudo systemctl restart docker`:

```json
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
```

## 3. Point a host name at the VM

Caddy needs a domain whose DNS points at the VM, with ports 80 and 443 open. Create a free
subdomain at [Duck DNS](https://www.duckdns.org) and set it to the VM's public IP address. This
guide calls it `your-name.duckdns.org`.

## 4. Start the backend

Clone the repository on the VM and enter it. Create `.env`, then set your own long random
passwords. Use letters, digits and hyphens only, because they're placed inside connection URLs.

```bash
cp .env.example .env
```

Leave `API_PORT` at `8000`, because `deploy/Caddyfile` expects it. Do not set `FAKE_DMS_FAULTS`.

Build, migrate and start the stack:

```bash
make up
```

Load the fictional data. The API key is printed once and only its hash is stored, so save it
somewhere private. Treat it as a password, and never commit it.

```bash
make seed
```

## 5. Publish the review page

On your own machine, build the page:

```bash
npm --prefix web ci && npm --prefix web run build
```

Copy it to the VM, replacing `USER` and `VM_ADDRESS`:

```bash
scp -r web/dist USER@VM_ADDRESS:/tmp/mailroom-web
```

On the VM, put it where Caddy serves it:

```bash
sudo rm -rf /srv/mailroom && sudo mkdir -p /srv && sudo mv /tmp/mailroom-web /srv/mailroom
```

## 6. Configure Caddy

On the VM, from the repository, install the versioned configuration with your host name:

```bash
sed 's/your-name.duckdns.org/REPLACE_WITH_YOUR_HOST/' deploy/Caddyfile | sudo tee /etc/caddy/Caddyfile
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
```

```bash
sudo systemctl reload caddy
```

Caddy obtains and renews the certificate itself.

## 7. Verify

Replace `HOST`, `KEY` and `ID` below.

**Exposure:**

- [ ] `https://HOST/` loads with a valid certificate.
- [ ] `curl -i https://HOST/api/healthz` returns 200.
- [ ] `curl -i https://HOST/api/readyz` returns 404.
- [ ] `curl -i https://HOST/api/reviews` returns 401.
- [ ] `curl -i https://HOST/api/docs` returns 404.
- [ ] From another machine, ports 5432, 5672 and 8000 can't be reached.

On the VM, this should show both dependencies as `ok`:

```bash
curl -s http://127.0.0.1:8000/readyz
```

**The full flow.** Uploads happen through the API, because the review page has no upload feature.

```bash
printf 'Complaint for damages.\nClient: Brightwater Cooperative\nRun: verify-1\n' > sample.txt
```

```bash
curl -H "X-API-Key: KEY" -F file=@sample.txt https://HOST/api/documents
```

- [ ] The response is 202 with an id. Sending the same file again returns 200 with
  `"duplicate":true`.
- [ ] Open `https://HOST/`, enter the key, and find the document as `AMBIGUOUS_MATCH`.
- [ ] Assign either matter.
- [ ] The document reaches `FILED`, and the audit trail shows three `FILING_STEP_COMPLETED`
  events followed by `FILED`:

```bash
curl -s -H "X-API-Key: KEY" https://HOST/api/documents/ID
```

```bash
curl -s -H "X-API-Key: KEY" https://HOST/api/documents/ID/events
```

**Recovery:**

- [ ] Reboot the VM. All services return, and a new document reaches `FILED`.
- [ ] Restore a backup at least once, as described below.

## 8. Back up

Back up the database and the documents together, because each refers to the other. RabbitMQ
needs no backup: the sweeper re-dispatches active documents from Postgres.

Run these on the VM, from the repository:

```bash
mkdir -p ~/mailroom-backups
```

```bash
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > ~/mailroom-backups/database.dump
```

```bash
docker compose exec -T api tar -C /data/documents -czf - . > ~/mailroom-backups/documents.tar.gz
```

Then copy them off the VM from your own machine. A backup kept only on the VM is lost with it.

```bash
scp -r USER@VM_ADDRESS:mailroom-backups ./mailroom-backups
```

## 9. Restore

Start from a running stack, on the same VM or a new one, with `make up` already done. Copy the
two backup files to `~/mailroom-backups`, then run these from the repository.

Stop the services that use the data:

```bash
docker compose stop api relay worker
```

Restore the database, replacing the current contents:

```bash
docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' < ~/mailroom-backups/database.dump
```

Start the API, which owns the document volume, and restore the documents through it:

```bash
docker compose start api
```

```bash
docker compose exec -T api tar -C /data/documents -xzf - < ~/mailroom-backups/documents.tar.gz
```

Start everything else:

```bash
docker compose start relay worker
```

Restored API keys keep working, so `make seed` isn't needed. Documents that were in progress are
picked up by the sweeper within a few minutes.

## Updating

For a backend change, pull on the VM, then run `make up`, which also applies migrations. For a
page change, repeat step 5. Back up first.

## If Oracle reclaims the VM

Create a new VM, follow steps 2 to 6, restore your latest backup, and point the Duck DNS subdomain
at the new address. Anything since that backup is lost.
