# Open5GS lab patches (Smart Farming)

## 0001-amf-select-slice-by-dnn.patch (Open5GS v2.7.5, AMF only)

Open5GS 2.7.5 has no URSP, so the OnePlus cannot know which S-NSSAI serves a DNN: it asks for DNN `crop`/`drone`
with S-NSSAI SD 000001 (or none) and its Allowed NSSAI holds only SD 000001, and the stock AMF answers
`DNN_NOT_SUPPORTED_OR_NOT_SUBSCRIBED_IN_THE_SLICE`. The patch adds a fallback, used only when the stock selection
fails: pick the subscribed slice whose session DNN matches and that the gNB supports in the TA, add it to the
UE's Allowed NSSAI and log `Smart Farming: DNN[...] mapped to S_NSSAI[...]`. Stock behaviour is unchanged when
the stock selection succeeds (N3IWUE sends the S-NSSAI explicitly and keeps working as before).

v2 (needed in practice): the phone released the mapped session ~100 ms after setup because SD 000002 was not in the
Allowed NSSAI it had stored. `amf_update_allowed_nssai()` now also allows every TA-supported subscribed S-NSSAI
marked as default, so the Registration Accept carries SD 1/2/3 for the OnePlus (all three are default in its
subscription) and nothing changes for the UN3GPP UE (only SD 000001 is default). After installing it, the phone needs
one re-registration (the AMF restart causes it) to learn the new Allowed NSSAI.

## Our Open5GS: `github.com/diegopivoto/open5gs`, branch `oran-tvws-5g`

The exact Open5GS of the lab is kept in our own private repository, so that installing it never depends on the
Open5GS PPA (which no longer serves 2.7.5, see the note at the end of the next section):

- branch `oran-tvws-5g` (default branch) = official history up to tag `v2.7.5` (also pushed) + **one** commit,
  `feat(amf): select the slice from the requested DNN (Smart Farming lab)`, which applies this patch (v2) and adds the
  `debian/changelog` entry `2.7.5~noble+oran.tvws.5g1`. The tree of `src/` is identical to
  `~/src/open5gs-2.7.5-sf` on PC1 (the source of the AMF that runs there);
- release `v2.7.5-oran-tvws-5g1`: the Ubuntu 24.04 (noble, amd64) `.deb` packages built from that commit (every
  `open5gs-*` package of the stock set, version `2.7.5~noble+oran.tvws.5g1`, same file lists as the PPA 2.7.5
  packages) + `SHA256SUMS`. GitHub replaces the `~` of the file names with `.`
  (`open5gs-amf_2.7.5.noble+oran.tvws.5g1_amd64.deb`); `SHA256SUMS` lists the published names.

**New machine (recommended path):** install those packages; their `/usr/bin/open5gs-amfd` is already the patched AMF,
so there is no `/opt/open5gs-sf` and no systemd drop-in. Step 1 of "New machine for the Smart Farming lab" in
`open5gs-core-lab/README.md` has the commands (`gh release download v2.7.5-oran-tvws-5g1 -R diegopivoto/open5gs`,
`sha256sum -c SHA256SUMS`, `sudo apt install ./open5gs*_2.7.5*.deb`, `apt-mark hold`). `install_amf_patch.sh` then
reports `patched AMF from the oran-tvws-5g packages: nothing to do` (it recognises the `+oran.tvws.5g` version suffix
or the `Smart Farming: DNN` string in `/usr/bin/open5gs-amfd`). The version sorts above the stock `2.7.5~noble` and
below `2.8.0~noble5`, so on a machine that still has the PPA the hold is what keeps 2.8.0 out.

PC1 itself still runs the PPA 2.7.5 packages with the AMF in `/opt/open5gs-sf` (the path below); it was not moved to
the new packages.

### Rebuilding the packages (other Ubuntu release, new patch revision)

```bash
git clone -b oran-tvws-5g https://github.com/diegopivoto/open5gs.git ~/src/open5gs-oran-tvws-5g
mkdir -p ~/src/open5gs-oran-tvws-5g-build
git -C ~/src/open5gs-oran-tvws-5g archive --prefix=open5gs-2.7.5/ HEAD | tar -x -C ~/src/open5gs-oran-tvws-5g-build
cd ~/src/open5gs-oran-tvws-5g-build/open5gs-2.7.5
sudo apt install debhelper && dpkg-checkbuilddeps   # install whatever else it lists
meson subprojects download    # dh runs meson with --wrap-mode=nodownload
CC=gcc-13 CXX=g++-13 DEB_BUILD_OPTIONS=parallel=4 nice -n 19 dpkg-buildpackage -b -us -uc   # .debs in ..
```

- On a machine running the OAI gNB (PC1), keep `nice -n 19` and at most 4 jobs.
- `CC=gcc-13`: on PC1 `cc` is gcc-11 (alternative used by other builds), whose built-in `_FORTIFY_SOURCE=2` clashes with
  the `-D_FORTIFY_SOURCE=3` of the noble hardening flags (`-Werror` in `lib/metrics`). On another release use its
  default gcc.
- The meson subprojects are branches, not pinned commits; the release was built with freeDiameter `539ca5f`
  (branch `r1.5.0`), prometheus-client-c `a58ba25` (branch `open5gs`), libtins `bf22438`, usrsctp `07f871b`
  (the same freeDiameter and prometheus-client-c commits as `~/src/open5gs-2.7.5-sf` on PC1).
- For a new revision: add a `debian/changelog` entry (`2.7.5~noble+oran.tvws.5g2`, ...; the source format is
  `3.0 (native)`, so the version must not contain `-`) and publish a new release `v2.7.5-oran-tvws-5g2`.

## Install the AMF beside stock 2.7.5 packages (PC1, reinstall): `install_amf_patch.sh`

Prerequisite: Open5GS **2.7.5** installed from apt (the script refuses any other version, see the note below). With
our packages (above) the script stops at step 1 with `nothing to do`. Run as the normal user; the script builds in
`--src` and calls `sudo` only for `/opt/open5gs-sf`, systemd and apt.

```bash
deployment/open5gs/install_amf_patch.sh --dry-run   # what is missing or differs; changes nothing
deployment/open5gs/install_amf_patch.sh             # clone + patch + build + install + drop-in (no restart)
deployment/open5gs/install_amf_patch.sh --restart   # same, then restarts open5gs-amfd
deployment/open5gs/install_amf_patch.sh --rollback  # back to the apt AMF (removes the drop-in)
```

It is idempotent (each step is skipped when already done; on PC1 a rerun ends with `already installed, up to date`):

1. checks `dpkg` and `open5gs-amfd -v` for 2.7.5; if the installed AMF is the patched `oran-tvws-5g` build (version
   suffix `+oran.tvws.5g` or the `Smart Farming: DNN` string in `/usr/bin/open5gs-amfd`) it reports `patched AMF from
   the oran-tvws-5g packages: nothing to do` and stops (warning if an old `/opt` drop-in still overrides the unit:
   remove it with `--rollback --restart`); otherwise it checks the build dependencies (Build-Depends of Open5GS
   2.7.5); the missing ones are printed as an `apt-get install` command, installed only with `--install-deps`;
2. when `--src` (default `~/src/open5gs-2.7.5-sf`) is absent, clones `https://github.com/diegopivoto/open5gs.git`
   branch `oran-tvws-5g` (patch already committed; private, so it needs GitHub credentials); if that clone fails, or
   with `--from-upstream`, it clones the official `https://github.com/open5gs/open5gs.git` at tag `v2.7.5` instead.
   The patch is applied with `git apply` unless it is already applied (stops if the tree carries a different version
   of the patch);
3. `meson setup build --prefix=/opt/open5gs-sf` (the first setup downloads the meson subprojects) and
   `ninja -C build src/amf/open5gs-amfd` (AMF only);
4. installs the binary and the Open5GS libraries it links from the build tree (`ldd`) into `/opt/open5gs-sf/{bin,lib}`
   with `install ... .new && mv -f` (a plain `cp` onto the running binary fails with "Text file busy");
5. writes `/etc/systemd/system/open5gs-amfd.service.d/smartfarm-dnn-slice.conf` and runs `daemon-reload`;
6. verifies: `systemctl cat` shows the override, `ldd` resolves every library (Open5GS ones from
   `/opt/open5gs-sf/lib`), the binary prints `Open5GS v2.7.5`, and whether the running AMF is the installed one.

The AMF is **not** restarted unless `--restart` is given; the script prints the restart command instead. Restarting
the AMF drops the N2 associations: the OAI gNB re-associates by itself; the N3IWF does not and must be restarted on
PC2 (tmux `n3iwf`), followed by the N3IWUE on PC3.

**Open5GS 2.7.5 from apt.** The `ppa:open5gs/latest` PPA now publishes 2.8.0 (candidate on PC1:
`2.8.0~noble5`); Launchpad marks the 2.7.5 builds as superseded and removed, so `apt install open5gs` on a new
machine no longer gives 2.7.5. On PC1, which stays at 2.7.5, the packages are held so an upgrade does not move the
other NFs: `sudo apt-mark hold $(dpkg-query -W -f='${Package}\n' 'open5gs*')`. A new machine installs the 2.7.5
packages of our release `v2.7.5-oran-tvws-5g1` instead (section above).

## Manual steps (reference: what the script does)

```bash
# source: v2.7.5 (PC1: release tarball in ~/src/open5gs-2.7.5-sf, a git repo with the patch on top of the release
# commit; the script clones diegopivoto/open5gs branch oran-tvws-5g, or the v2.7.5 tag + this patch, same tree)
git -C ~/src/open5gs-2.7.5-sf apply deployment/open5gs/0001-amf-select-slice-by-dnn.patch
cd ~/src/open5gs-2.7.5-sf
meson setup build --prefix=/opt/open5gs-sf && ninja -C build src/amf/open5gs-amfd
sudo install -m 0755 build/src/amf/open5gs-amfd /opt/open5gs-sf/bin/open5gs-amfd.new
sudo mv -f /opt/open5gs-sf/bin/open5gs-amfd.new /opt/open5gs-sf/bin/open5gs-amfd
ldd build/src/amf/open5gs-amfd | awk '/open5gs-2.7.5-sf/ {print $3}' | xargs -I{} sudo cp -L {} /opt/open5gs-sf/lib/
# systemd drop-in /etc/systemd/system/open5gs-amfd.service.d/smartfarm-dnn-slice.conf:
#   [Service]
#   Environment=LD_LIBRARY_PATH=/opt/open5gs-sf/lib
#   ExecStart=
#   ExecStart=/opt/open5gs-sf/bin/open5gs-amfd -c /etc/open5gs/amf.yaml
sudo systemctl daemon-reload
```

Rollback (what `--rollback --restart` does): `sudo rm
/etc/systemd/system/open5gs-amfd.service.d/smartfarm-dnn-slice.conf && sudo systemctl daemon-reload && sudo systemctl
restart open5gs-amfd` (back to the PPA binary; `/opt/open5gs-sf` can stay).
