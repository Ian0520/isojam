# Private AWS test deployment

This guide prepares one start/stop GPU host in **ap-southeast-2 (Sydney)**.
The first AWS test uses the same API/importer/processor images as the local
stack. Recorded cloud verification and its limits appear at the end of this guide.
Public HTTPS and access for friends are a later stage.

## Design and cost

`deploy/aws/ec2.yaml` creates one `g4dn.xlarge`, its SSM instance profile and a
security group with **no inbound rules**. Use an existing public subnet with an
internet-gateway route. The temporary public IPv4 address provides outbound
access for SSM, dependencies and model downloads; it does not expose the API.
No SSH key, NAT gateway, load balancer, managed database or image registry is
required for this first test.

The CPU API, CPU importer and GPU processor use the existing Compose services.
Keeping them on one host preserves the current SQLite, filesystem locking and
worker-process coordination assumptions. One separation runs at a time; other
accepted jobs wait in the database. The cloud environment limits each user to
one unfinished separation and one unfinished import, with two imports globally.

Prices checked against the AWS Price List API on 2026-10-21, in USD before
credits, tax, snapshots and transfer outside allowances:

| Item | Rate |
| --- | ---: |
| g4dn.xlarge Linux On-Demand | $0.684/running hour |
| Temporary public IPv4 | $0.005/running hour |
| 100 GB gp3, retained for a full month | $9.60/month |
| Example: 10 running hours plus a month of storage | $16.49 |
| Example: 20 running hours plus a month of storage | $23.38 |

These are estimates, not a spending ceiling. Building images and downloading
weights also consume running time. Stopping the instance stops compute charges;
the retained disk continues to cost money. See [EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/),
[EBS pricing](https://aws.amazon.com/ebs/pricing/) and
[IPv4 pricing](https://aws.amazon.com/vpc/pricing/).

The template installs a two-hour timer at first boot and enables it on each
later boot. It first stops `isojam.service`, allowing the normal Compose drain,
then requests OS poweroff. EC2 interprets that as **stop**, not termination.
The timer includes setup time and is not an inactivity detector. Shutdown can
take up to 40 additional minutes. If application shutdown fails, the timer still
powers off the host; investigate unfinished jobs rather than retrying blindly.
Verify the timer after launch: failed cloud-init or a disabled timer defeats
this safeguard. Also configure an appropriate spend limit in AWS Settings > Billing.
`SessionMinutes` sets the timer during initial cloud-init only. Changing that
parameter on an existing stack does not rewrite an already installed timer;
review and update the host timer explicitly if a different duration is needed.

## Prerequisites and launch review

Before creating resources:

1. Confirm the Paid plan and selected Region in AWS Settings > View all projects
   > Overview > Additional Info > Region. Use `ap-southeast-2` throughout.
2. Wait for at least **4 vCPUs** in the EC2 quota **Running On-Demand G and VT
   instances**, code `L-DB2E81BA`. Credits do not grant GPU quota or capacity.
3. Choose a public subnet in the same VPC passed as `VpcId`. Confirm DNS is
   enabled and the subnet uses an active `0.0.0.0/0` internet-gateway route.
4. Validate the template with cfn-lint for Sydney and cfn-guard security rules,
   then review a CloudFormation change set before executing it. Inspect IAM
   permissions and resource replacements explicitly.
5. Review the hourly and retained-storage costs above. Launch starts billing.

The default AMI is the AWS-owned **Deep Learning Base OSS NVIDIA Driver GPU AMI
(Ubuntu 24.04), 20261016**, `ami-06567ae981b6477cd` in Sydney. It was available when
checked. Recheck its owner, availability and `/dev/sda1` root mapping before use.
Do not substitute an arbitrary AMI or Region without reviewing its driver,
root-disk mapping and storage layout.

The CloudFormation resources and their design constraints are documented here
and in template metadata. The role trusts EC2 and attaches only the AWS-managed
`AmazonSSMManagedInstanceCore` policy; application containers do not need AWS
credentials. IMDSv2 is required with a hop limit of one. The security group
permits outbound TCP 80/443 and no inbound traffic.

Use the AWS MCP tools for an agent-driven launch. For manual AWS CLI operations,
use **Windows PowerShell**, where the `isojam` profile is configured, and include
`--profile isojam --region ap-southeast-2` on every command. Record the stack's
`InstanceId` output after an approved launch.

## Connect privately

For the first shell, use EC2 > Instances > Connect > Session Manager in Sydney.
Wait for SSM to report the instance online. It needs a working agent, instance
profile and outbound internet access, but no inbound port 22.

For the browser, install the local
[Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
on the machine running AWS CLI. In **Windows PowerShell**, replace the instance
ID below with the stack output and leave the tunnel running:

```powershell
$instanceId = 'i-REPLACE-WITH-STACK-OUTPUT'
aws ssm start-session --profile isojam --region ap-southeast-2 --target $instanceId --document-name AWS-StartPortForwardingSession --parameters 'portNumber=8000,localPortNumber=8001'
```

Once the API is healthy, open `http://localhost:8001` on that machine. Local port
8001 avoids colliding with a local IsoJam on 8000. Closing the browser or tunnel
does **not** stop the EC2 instance. See the
[SSM port-forwarding documentation](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-sessions-start.html).

## First host setup

The following commands run **on the EC2 host**, not on the laptop. Enter a root
shell with `sudo -i`. First inspect the AMI rather than replacing its drivers:

```bash
cloud-init status --wait
systemctl list-timers isojam-session-end.timer
systemctl cat isojam-session-end.service
nvidia-smi
lsblk -o NAME,SIZE,MODEL,SERIAL,FSTYPE,MOUNTPOINTS
df -h /
docker version
docker compose version
nvidia-ctk --version
```

The application needs Linux x86_64, a CUDA 13-compatible driver (at least
580.65.06), Docker Engine, Compose with `gpus` support, and NVIDIA Container
Toolkit configured for Docker. The GPU image supplies Torch/CUDA userspace.
If Docker or the toolkit is missing, use the official
[Docker Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/) and
[NVIDIA toolkit instructions](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
Resolve missing prerequisites before continuing; the cloud AMI runtime still
needs verification on the first instance.

Inspect `docker info --format '{{.DockerRootDir}}'` and use `findmnt -T` on that
path. Both Docker's data root (including containerd image storage, when used)
and `/srv/isojam` must reside on the **persistent EBS root disk**, identified by
its `vol...` serial. Some DLAMI layouts use ephemeral NVMe for Docker. Stop here
and relocate that state while Docker is stopped if necessary. A GPU instance's
local instance-store NVMe is lost across stop/start; a filesystem path alone
does not prove persistence.

Clone a reviewed, merged revision, then create durable directories. Do not
overwrite an existing checkout or runtime settings:

```bash
git clone https://github.com/Ian0520/isojam.git /opt/isojam
cd /opt/isojam
git rev-parse HEAD
install -d -m 0755 -o 10001 -g 10001 /srv/isojam/data /srv/isojam/models
install -d -m 0700 /etc/isojam
python3 - <<'PY'
from pathlib import Path
import os
import secrets

path = Path('/etc/isojam/runtime.env')
settings = Path('deploy/aws/runtime.env.example').read_text()
settings = settings.replace('ISOJAM_JWT_SECRET_KEY=\n',
                            f'ISOJAM_JWT_SECRET_KEY={secrets.token_hex(32)}\n')
# Exclusive creation prevents an accidental rerun from replacing a stable secret.
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as output:
    output.write(settings)
PY
```

Keep the file private and preserve the generated secret. Do not print it into
shared logs or put it in CloudFormation parameters, metadata, user data or Git.
For first-time account creation, edit the file to set
`ISOJAM_REGISTRATION_MODE=invite`; use the owner invitation command below. The
default remains closed until this deliberate change.

Build images and download the pinned weights before starting the application:

```bash
deploy/aws/stack.sh build
deploy/aws/stack.sh models
docker run --rm --gpus all isojam-processor:local-stack nvidia-smi
install -m 0644 deploy/aws/isojam.service /etc/systemd/system/isojam.service
systemctl daemon-reload
systemctl start isojam.service
deploy/aws/stack.sh status
curl --fail http://127.0.0.1:8000/health
```

`models` writes only to `/srv/isojam/models`; the processor mounts weights
read-only. `start` uses existing images and waits for healthy services. The
oneshot systemd unit remains active so ordinary shutdown can stop the stack;
it also stops partial containers if startup fails. It is deliberately not
enabled for automatic application startup on boot. Check `status` for later
container failures; systemd's active state alone does not continuously monitor
Compose health or restart failed workers.

To issue your invitation without printing it into agent/tool logs, run in your
own host terminal (replace the email):

```bash
docker exec isojam-aws-api-1 python -m app.invitations create you@example.com
```

Redeem the displayed code through the private browser tunnel. Existing local
accounts are not copied to AWS. Read [invitations](invitations.md) for code and
transaction behavior. You can return registration to `closed` and restart the
service after creating your account.

## Start and stop later sessions

Start the existing instance in the EC2 console in Sydney; do not create another
stack. Reconnect using SSM, verify the boot timer, then run on the host:

```bash
sudo systemctl start isojam.service
sudo /opt/isojam/deploy/aws/stack.sh status
```

When finished, **drain the application before stopping the instance**. On the
host, run these commands sequentially; only power off if application stop
succeeds:

```bash
sudo systemctl stop isojam.service && sudo systemctl poweroff
```

Wait for EC2 to report **stopped**. Do not use force stop as the normal workflow.
The API stops admission, the importer drains, and the processor gets its existing
35-minute grace period to finish and publish active work. Compose shutdown
preserves service grace periods. Do not shorten them with `stop -t` or `down -t`.
The timer performs the same drain before poweroff, even if no terminal is open.

For an application-only restart, `sudo systemctl restart isojam.service` drains
then recreates containers. This does not reset the boot timer. Application
configuration changes require container recreation. Failed startup leaves a
failed systemd unit and stops partial containers; inspect
`journalctl -u isojam.service` and container logs before trying again.

## Persistence, updates and cleanup

`deploy/aws/compose.aws.yaml` binds the shared database/audio directory to
`/srv/isojam/data` and weights to `/srv/isojam/models`. These replace the local
named-volume mounts without changing local Compose usage. Never run the base
Compose file alone on this host: it would select different storage and settings.

The template retains the encrypted 100 GB root EBS volume even if the instance
is terminated. This protects data but does not automatically attach old data to
a replacement host, provide a backup or stop storage billing. Deleting a stack
removes the compute and IAM/network resources it created, but leaves this disk
behind. Replacement updates can also leave retained disks. Inventory retained
volumes and delete only after a deliberate backup/cleanup decision.

Before application updates, drain the stack, back up the stopped database and
audio together to storage outside this instance, record the current commit and
image IDs, then build the reviewed revision. Schema migrations are not generally
reversible; restoring an older image alone does not roll back a database change.
Do not update the AMI, subnet or other replacement properties in place without
a separate data-recovery plan. The current cloud setup has no automated backup
or public HTTPS endpoint.

## Verification required before calling this deployed

- Verify host driver/runtime compatibility and a real separation through the
  private browser, including all seven authenticated downloads.
- Stop through systemd during inference: the active job must finish and publish,
  while queued jobs remain recoverable.
- Stop/start the EC2 instance and verify the account, job, upload hash, seven
  result downloads, model cache and images persist.
- Verify the session timer requests orderly stop, then confirm EC2 is stopped.
- Check actual charges/credits and retained storage in AWS Settings > Billing.

Local template, Compose and wrapper checks cannot prove any of these cloud
runtime properties. Record real cloud results here after executing the test.

## Configuration checks

The AWS configuration workflow uses no AWS credentials and creates no resources.
It runs the wrapper's subprocess tests, Compose configuration validation,
**cfn-lint 1.58.0** for Sydney, and **CloudFormation Guard 3.2.1**. The Guard
release archive is checksum-verified before execution.

`deploy/aws/private-test.guard` checks this stage's specific constraints:
IMDSv2, encrypted/retained root storage, no inbound rules, the reviewed GPU/disk
size, and stop-on-poweroff. These are selected policy checks, not a complete AWS
security audit. The official
[AWS Guard rules registry](https://github.com/aws-cloudformation/aws-guard-rules-registry)
offers additional checks; its `ENCRYPTED_VOLUMES` rule covers standalone EBS
resources, so this project also checks the inline EC2 root block mapping.

With the tools installed, run from the repository root in Linux/WSL:

```bash
python3 -m unittest discover -s deploy/aws/tests -v
cfn-lint --regions ap-southeast-2 --template deploy/aws/ec2.yaml
cfn-guard validate --rules deploy/aws/private-test.guard --data deploy/aws/ec2.yaml --output-format json --show-summary none
```

AWS `ValidateTemplate` checks template structure; it does not check GPU quota,
available capacity, subnet connectivity or driver compatibility. A reviewed
change set and the real private test remain required before deployment sign-off.

### Local verification on 2026-10-21

The five wrapper tests, Python lint/format checks, shell syntax checks and merged
Compose configuration check passed. `systemd-analyze verify` accepted the app
unit and the units extracted from user data, using temporary executable paths
and a stub Docker dependency; no services were installed or started locally.

cfn-lint reported no findings, AWS `ValidateTemplate` accepted the template, and
all four project Guard policies passed on Windows and Linux. Five disposable
negative templates were rejected for public ingress, unencrypted storage,
optional IMDSv2, a larger disk and termination-on-poweroff. These checks prove
configuration behavior, not a successful cloud launch. GitHub workflow status
was not part of these local checks.

### First private AWS run on 2026-10-21

The private stack launched in Sydney using the template's AWS DLAMI on one
`g4dn.xlarge`. The host reported an NVIDIA Tesla T4 with 15,360 MiB available,
driver 595.91.07, Docker Engine 29.9.1, Compose 5.6.0 and NVIDIA Container Toolkit
1.20.1. These host tools were already installed in the AMI. The SSM role and
instance profile were attached, IMDSv2 was required, and the security group had
no inbound rules. The two-hour timer was active after initial cloud-init.

The encrypted 100 GB gp3 root EBS disk contained `/srv`, `/var/lib/docker` and
`/var/lib/containerd`. The AMI also had a separate ephemeral NVMe filesystem at
`/opt/dlami/nvme`; it was not used for application state or images. About 32 GB
remained free after the initial builds. This establishes room for the test,
not unlimited recording retention or a long-term storage estimate.

The three images built from application revision `69dc2fd`. The pinned model
checkpoint downloaded and passed SHA-256 verification. The API, importer and
processor became healthy, and the browser page, `/health` and `/ui-info` loaded
through an SSM tunnel to local port 8001. Registration used `invite` mode.
The JWT secret was generated on the host in a root-only runtime settings file.

The first automated setup exposed an input-handling bug: Compose's one-off
model container kept stdin open and consumed the remainder of the shell script.
The command exited successfully even though the later service-start command
never ran. Revision `de687d0` makes model setup explicitly noninteractive and
adds a regression test that preserves a parent shell's pending input. All six
wrapper tests and Python lint/format checks passed. The exact revision was
transferred as a Git bundle and checked out on the host without publishing the
branch; no application image rebuild was needed for this host-wrapper change.

The cloud smoke created a synthetic test account through invitation redemption,
uploaded 30 seconds of generated stereo audio and submitted a queued job. A
repeated request with the same idempotency key returned the same job. Shutdown
was requested after inference was observed in the worker log. The stack drained
in 16.09 seconds; the processor exited zero, the job completed with all seven
outputs, and its single attempt had recorded successful exit proof. The stored
upload hash matched the submitted WAV. Test credentials stayed in a root-only
host file and were not printed into command output or committed.

These checks use synthetic tones, not a music-quality benchmark. They do not
establish performance for ten-minute recordings, five simultaneous users or
YouTube downloads from AWS addresses.

### Spend-limit interruption and remaining verification

After the application drained, a normal EC2 stop completed and the same instance
started again. SSM returned online, but the application-level persistence check
had not run when the session was interrupted. At 13:12:22 UTC on 2026-10-21,
CloudTrail recorded another `StopInstances` call from the AWS-managed
`AWSServiceRoleForBudgetsSpendLimitMember` role. A subsequent start request was
denied by a managed service control policy with a `DenyCompute` statement.
The host remained stopped with its encrypted root disk retained.

The project still reported the Paid plan and $200 in remaining credits. That
does not override a spend limit: AWS applies spend limits to pre-tax costs
before credits and can pause resources through early cost controls. Project
owners can inspect the project's spend status and controls in
[AWS Settings > Billing](https://settings.aws.com/). If the project is paused,
AWS documents increasing its spend limit as the reactivation step. Review the
amount and resulting cost exposure before changing it; do not remove managed
policies or create replacement resources to bypass the control. See
[AWS spend-limit documentation](https://docs.aws.amazon.com/accounts/latest/reference/create-spend-limit.html).

This run has **not** completed deployment verification. Once the project permits
starting the existing host, finish these checks:

- Verify the database integrity, saved account, job and attempt, upload hash,
  all seven result hashes, model cache and image IDs after stop/start.
- Download all seven results with authentication and verify unauthenticated
  requests are rejected.
- Recheck the boot timer, exercise its orderly shutdown action and confirm
  EC2 reaches `stopped`. An active timer alone does not prove its action works.
- Check actual charges and credit application in AWS Settings > Billing.

The private tunnel is closed while the host is stopped, so `localhost:8001`
does not currently serve the AWS application. Stopped compute has no instance
running-hour charge; the retained 100 GB disk continues to incur storage cost.
