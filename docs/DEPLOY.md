# Deploying a public demo

This guide runs the whole stack on one Oracle Cloud Always Free VM. The review page and API
share one HTTPS origin through Caddy.

**Status:** no real Oracle deployment has been tested. The Caddy routing, restart recovery,
and backup and restore procedures were rehearsed locally on an arm64 Docker host, over plain
HTTP. Real TLS, DuckDNS, Oracle's firewall, the 2 GB VM size and a real VM reboot have not been
exercised.

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
2. Create an Always Free Arm instance, `VM.Standard.A1.Flex`, with an Ubuntu `aarch64` image and
   SSH key login. Start with 1 OCPU and 2 GB of memory. The stack idled at about 465 MiB when
   measured locally, but this size hasn't been tested on a real VM. If the build runs out of
   memory, use a larger shape.
3. If you see "out of host capacity", try another availability domain or try again later.
4. Allow inbound TCP 22 from your IP address only, and 80 and 443 from anywhere, in the
   instance's network security rules.

Do not open 5432, 5672 or 8000.

Oracle's idle rule includes memory use below 20%, which is why a smaller VM is suggested. Oracle
doesn't say how it measures this, so treat it as a precaution, not a guarantee.

## 2. Install the software

Follow the official guides for [Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
and [Caddy](https://caddyserver.com/docs/install). Then install `git` and `make`, let your user
run Docker, and start Docker at boot:

```bash
sudo apt-get install -y git make
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

## 3. Open the VM's own firewall

Oracle's network rules aren't enough. Oracle's Ubuntu images also use `iptables`, so ports 80 and
443 must be opened on the VM too. Oracle advises against relying on UFW here. Do this now, because
Caddy can't get a certificate while port 80 is blocked, and Let's Encrypt allows only 5 failed
attempts per host per hour.

Find the `REJECT` rule in the `INPUT` chain:

```bash
sudo iptables -L INPUT --line-numbers
```

Insert both rules above it, replacing `POSITION` with that rule's line number. Rules added below
`REJECT` have no effect.

```bash
sudo iptables -I INPUT POSITION -m state --state NEW -p tcp --dport 80 -j ACCEPT
```

```bash
sudo iptables -I INPUT POSITION -m state --state NEW -p tcp --dport 443 -j ACCEPT
```

Save them, so they survive a reboot:

```bash
sudo netfilter-persistent save
```

From your own machine, confirm that port 80 answers before you go further. Caddy's default page
should respond, replacing `VM_ADDRESS`:

```bash
curl -I http://VM_ADDRESS
```

These commands come from Oracle's tutorials and haven't been tested here, so check them against
your image. Docker adds its own rules, so also confirm that containers can talk to each other
once the stack is running.

## 4. Point a host name at the VM

Caddy needs a domain whose DNS points at the VM, with ports 80 and 443 open. Create a free
subdomain at [Duck DNS](https://www.duckdns.org) and set it to the VM's public IP address. This
guide calls it `your-name.duckdns.org`.

## 5. Start the backend

Clone the repository on the VM and enter it, replacing `REPOSITORY_URL`:

```bash
git clone REPOSITORY_URL mailroom && cd mailroom
```

Create `.env`, generate long random passwords, and make the file private. Hexadecimal values are
safe here, because passwords are placed inside connection URLs.

```bash
cp .env.example .env
```

```bash
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 32)/; s/^RABBITMQ_PASSWORD=.*/RABBITMQ_PASSWORD=$(openssl rand -hex 32)/" .env
```

```bash
chmod 600 .env
```

The stack starts happily with the placeholder passwords, so check they're gone. This should print
`0`:

```bash
grep -c change-me .env
```

Leave `API_PORT` at `8000`, because `deploy/Caddyfile` expects it. Do not set `FAKE_DMS_FAULTS`.

Build, migrate and start the stack:

```bash
make up
```

Load the fictional data. The API key is printed once and only its hash is stored, so save it
somewhere private and off the VM. Treat it as a password, and never commit it.

```bash
make seed
```

## 6. Publish the review page

On your own machine, build the page. This needs Node `^20.19` or `>=22.12`.

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

## 7. Configure Caddy

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

Caddy is designed to obtain and renew the certificate itself. That hasn't been tested here, so
check it in the next step. If it fails, confirm ports 80 and 443 and the DNS record before trying
again, because of Let's Encrypt's limit on failed attempts.

## 8. Verify

Replace `HOST`, `KEY` and `ID` below.

**Exposure:**

- [ ] `https://HOST/` loads with a valid certificate.
- [ ] `curl -i https://HOST/api/healthz` returns 200.
- [ ] `curl -i https://HOST/api/readyz` returns 404.
- [ ] `curl -i https://HOST/api/reviews` returns 401.
- [ ] `curl -i https://HOST/api/docs` returns 404.
- [ ] From another machine, ports 5432, 5672 and 8000 can't be reached:

```bash
nc -vz -w 5 HOST 5432
```

```bash
nc -vz -w 5 HOST 5672
```

```bash
nc -vz -w 5 HOST 8000
```

On the VM, this should show both dependencies as `ok`:

```bash
curl -s http://127.0.0.1:8000/readyz
```

**Resources.** Check memory with the stack running. Oracle may treat a VM as idle if memory stays
below 20%, along with low CPU and network use:

```bash
free -m
```

```bash
docker stats --no-stream
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

## 9. Back up

Back up the database and the documents together, because each refers to the other. RabbitMQ
needs no backup: the sweeper re-dispatches active documents from Postgres.

Nothing does this automatically. Back up before each demo and before each update, and copy the
result off the VM. A backup kept only on the VM is lost with it.

Run these on the VM, from the repository, in one shell session. Keep this order: the database
goes first, so that every document it mentions is in the archive that follows.

```bash
BACKUP=~/mailroom-backups/$(date +%Y-%m-%d-%H%M) && mkdir -p "$BACKUP" && echo "$BACKUP"
```

```bash
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$BACKUP/database.dump"
```

```bash
docker compose exec -T api tar -C /data/documents -czf - . > "$BACKUP/documents.tar.gz"
```

On your own machine, copy it off the VM. Replace `BACKUP_NAME` with the folder's dated name.

```bash
mkdir -p mailroom-backups
```

```bash
scp -r USER@VM_ADDRESS:mailroom-backups/BACKUP_NAME mailroom-backups/
```

Your API key and the Caddy host name aren't in the backup. `.env` needn't be saved, because a new
VM generates new passwords, and restored data doesn't depend on the old ones.

## 10. Restore

Start from a running stack, on the same VM or a new one, with `make up` already done.

From your own machine, copy the backup to the VM, replacing `BACKUP_NAME`:

```bash
scp -r mailroom-backups/BACKUP_NAME USER@VM_ADDRESS:mailroom-restore
```

If `~/mailroom-restore` already exists on the VM, delete it first, or `scp` will nest the folder.

Run the rest on the VM, from the repository. Stop the services that use the data:

```bash
docker compose stop api relay worker
```

Delete the current database and create an empty one. This destroys its contents, so check you have
the right backup first. Restoring over the top isn't safe: anything added since the backup would be
left behind and collide with the migration below.

```bash
docker compose exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" --force "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
```

Restore the backup into it:

```bash
docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' < ~/mailroom-restore/database.dump
```

Bring the restored schema up to date. An older backup also brings back its older schema, so this
must run before anything starts:

```bash
make migrate
```

Start the API, which owns the document volume, and restore the documents through it:

```bash
docker compose start api
```

```bash
docker compose exec -T api tar -C /data/documents -xzf - < ~/mailroom-restore/documents.tar.gz
```

Start everything else:

```bash
docker compose start relay worker
```

Restored API keys keep working, so `make seed` isn't needed, and a key created on the new VM stops
working. A document that was in progress is picked up by the sweeper, which took about two minutes
in the one local rehearsal.

## Updating

For a backend change, pull on the VM, then run `make up`, which also applies migrations. For a
page change, repeat step 6. Back up first.

## If Oracle reclaims the VM

Oracle doesn't document exactly what reclaiming does, so first check in the console whether the VM
was stopped or deleted. If it was stopped, try starting it. An ephemeral public IP stays with a
stopped instance, but is deleted if the instance is terminated.

If it's gone:

1. Create a new VM and repeat steps 1 to 4. Skip the network rules if the old ones are still there,
   but always repeat the host firewall in step 3. Update the Duck DNS subdomain to the new address.
2. Repeat steps 5 to 7, skipping `make seed`.
3. Restore your latest backup with step 10, then run the checks in step 8.

Anything since that backup is lost.
