## [2.7.2](https://github.com/ExaDev/github-runner/compare/v2.7.1...v2.7.2) (2026-10-20)

### Documentation

* **autoscaler:** say why a runner pod's CPU request was not halved ([c0475c7](https://github.com/ExaDev/github-runner/commit/c0475c7f8cce7b60b3f97db0176cc9bd82c5e9f6))

## [2.7.1](https://github.com/ExaDev/github-runner/compare/v2.7.0...v2.7.1) (2026-10-20)

### Bug Fixes

* **cluster:** run the join wait from the image's filesystem ([e8d0607](https://github.com/ExaDev/github-runner/commit/e8d0607760f609b7a9db0eb55dba9441d54f2749)), closes [#116](https://github.com/ExaDev/github-runner/issues/116)

### Tests

* **mesh:** read a joining server's whole log for the wait's line ([105eb45](https://github.com/ExaDev/github-runner/commit/105eb45e520283bf9c248bb77e78b3c1c8d7ce5c))

## [2.7.0](https://github.com/ExaDev/github-runner/compare/v2.6.1...v2.7.0) (2026-10-20)

### Features

* **arc:** grant the autoscaler its ceiling read and make the ceiling a bound ([11e99e4](https://github.com/ExaDev/github-runner/commit/11e99e42187fb01c67c2189dcbd6f8f56703a105)), closes [#120](https://github.com/ExaDev/github-runner/issues/120)
* **autoscaler:** work the ceiling out from the nodes every poll ([5eaff58](https://github.com/ExaDev/github-runner/commit/5eaff58e9d55a9035bd906a6d6aa31a3c817110d)), closes [#120](https://github.com/ExaDev/github-runner/issues/120)

### Tests

* **autoscaler:** run the ceiling on a real cluster as its own service account ([4541601](https://github.com/ExaDev/github-runner/commit/454160137a54042906c8b2981df136c4e5683ed2)), closes [#120](https://github.com/ExaDev/github-runner/issues/120)

## [2.6.1](https://github.com/ExaDev/github-runner/compare/v2.6.0...v2.6.1) (2026-10-20)

### Bug Fixes

* **cluster:** report a first run's Compose start under --check ([e9bf55c](https://github.com/ExaDev/github-runner/commit/e9bf55c96ee03c45a8d987523abd396c465aa0c9)), closes [#122](https://github.com/ExaDev/github-runner/issues/122)

### Tests

* **mesh:** keep a stepped-down server at its level through a restart and a recreate ([a5b53d5](https://github.com/ExaDev/github-runner/commit/a5b53d53109cbd1c2ac7eeefef1bc7b5ec18f341))

## [2.6.0](https://github.com/ExaDev/github-runner/compare/v2.5.0...v2.6.0) (2026-10-20)

### Features

* **cluster:** apply regrade levels to a Docker node ([8284a4d](https://github.com/ExaDev/github-runner/commit/8284a4d081417ec42efd3f75003a13e7b6ca4021))
* **cluster:** let the cluster role run the regrade service ([ce4ff38](https://github.com/ExaDev/github-runner/commit/ce4ff38644c64a54237b9f9830fe4ef03bf226f8))
* **cluster:** let the regrade core step a server back up by itself ([a45e7fb](https://github.com/ExaDev/github-runner/commit/a45e7fb3d1d5d25c504cdcc21e7eb81ae17534f8))
* **cluster:** ship the platform-neutral regrade core and its tests ([7db7948](https://github.com/ExaDev/github-runner/commit/7db7948e74491c4df793efec5b7bfe2026a72157))
* **cluster:** stop the regrade service on a kernel without memory pressure ([acf2c7c](https://github.com/ExaDev/github-runner/commit/acf2c7cb9729d2c014098da24c5260f9ce623c87))
* **heartbeat:** publish a stepped-down server's level and spare it from the alert ([d69f7c7](https://github.com/ExaDev/github-runner/commit/d69f7c782505d735ad1cc87599e9eb72c69f49e7))

### Documentation

* **cluster:** name the Compose profile in the regrade service's commands ([bc34378](https://github.com/ExaDev/github-runner/commit/bc34378eb6fee1e9da75627bdf975511a419da77))

### Tests

* **cluster:** check the version arguments only of services built from the k3s context ([058480d](https://github.com/ExaDev/github-runner/commit/058480db14977f5750fe79e9546d187705fb2206))
* **cluster:** give the instance entrypoint harness the regrade flags ([6b6c2fd](https://github.com/ExaDev/github-runner/commit/6b6c2fd0a12f00e3166856da35cf84262dca2ece))
* **cluster:** give the regrade entrypoint harness the API port flag ([7f25f47](https://github.com/ExaDev/github-runner/commit/7f25f4755f1f6af4d340d2f5b195f0c880a7d114))
* **cluster:** order the regrade service after the serialised server start ([41d4940](https://github.com/ExaDev/github-runner/commit/41d49405de805828ff1bbe79b5bd558581023fd3))
* **mesh:** step a server down and back up through the regrade service ([80b65bf](https://github.com/ExaDev/github-runner/commit/80b65bfaba7e9eaafc98f1a6f2f08e7543af854e))
* **mesh:** stop the watcher by pid and judge a stepped-down rerun by the recap ([09b80a9](https://github.com/ExaDev/github-runner/commit/09b80a96a153516a19088a08dc60384eca3c18e4))
* **mesh:** wait for the refusal that names the stepped-down server ([0454e95](https://github.com/ExaDev/github-runner/commit/0454e9565480763a7709c2bae39fd2a0bab479c8))
* **regrade:** allow the indirectly called functions older shellcheck reports as unreachable ([44d3711](https://github.com/ExaDev/github-runner/commit/44d3711da7c25006a9c31c14251e633cc17f7966))

## [2.5.0](https://github.com/ExaDev/github-runner/compare/v2.4.0...v2.5.0) (2026-10-20)

### Features

* **cluster:** run a second cluster beside the first on the same Docker hosts ([2c103a9](https://github.com/ExaDev/github-runner/commit/2c103a9cc78810949202de5f04d6d689937aa006)), closes [#113](https://github.com/ExaDev/github-runner/issues/113)

### Bug Fixes

* **cluster:** describe a new Tailscale join key in characters the API accepts ([a6d2bee](https://github.com/ExaDev/github-runner/commit/a6d2bee73a15578a554a666e047b3837c04ea74e)), closes [#118](https://github.com/ExaDev/github-runner/issues/118)
* **cluster:** wait for the join host after k3s's own tailscale up ([6fae111](https://github.com/ExaDev/github-runner/commit/6fae111180da549e0d4f42b8cf10024acc964c39)), closes [#116](https://github.com/ExaDev/github-runner/issues/116)

### Tests

* **cluster:** give the etcd timing tests' entrypoint harness the API port flag ([7e6c245](https://github.com/ExaDev/github-runner/commit/7e6c245253e04c14baa63fa373e2fc05a2aeedeb))
* **mesh:** name each test node's container through the role's variable ([272cc6c](https://github.com/ExaDev/github-runner/commit/272cc6ca7894f7d444794f7a2d099bd98fcb0234))

## [2.4.0](https://github.com/ExaDev/github-runner/compare/v2.3.0...v2.4.0) (2026-10-20)

### Features

* **arc:** delete no node unless three Ready etcd members would remain ([bcfe065](https://github.com/ExaDev/github-runner/commit/bcfe0652d1cb856b3fed05ea324f372970ca1018))
* **arc:** let the node recovery watcher delete a labelled node that stayed Unknown ([80fa540](https://github.com/ExaDev/github-runner/commit/80fa540f516487147809daeda5d2de972d9d8675))

### Bug Fixes

* **arc:** confine the node-deleting watcher pod to control-plane nodes ([3d57b9e](https://github.com/ExaDev/github-runner/commit/3d57b9edc05d513977377ac8bf965f2a0f2e39fd))
* **arc:** default the prune label to the fleet's node label domain ([f15e146](https://github.com/ExaDev/github-runner/commit/f15e1469f325c77f7d9577e1e9f5689e1e60819a))

### Tests

* **arc:** keep the removable worker second so kind names it as the test expects ([282c05d](https://github.com/ExaDev/github-runner/commit/282c05d9b7dd52e1936982f89141ad9ed3ede4ae))
* **arc:** prune a stopped, labelled worker in the node recovery kind test ([17d70dd](https://github.com/ExaDev/github-runner/commit/17d70dda28535c26255a470baaa39525634c6bb9))
* **arc:** read each watcher pod's log on its own ([3de565d](https://github.com/ExaDev/github-runner/commit/3de565d5149ad6c44299b3b039261e4c8bb42c3a))
* **arc:** read the prune log from every watcher pod after the rollout ([364dfef](https://github.com/ExaDev/github-runner/commit/364dfefb9f3ea3de91a6441c2279b255233d6075))
* **arc:** wait for the prune threshold before expecting the voters refusal ([5463601](https://github.com/ExaDev/github-runner/commit/5463601791c8ecc7fe8e1b20e49fcc3ab5d2860b))

## [2.3.0](https://github.com/ExaDev/github-runner/compare/v2.2.0...v2.3.0) (2026-10-20)

### Features

* **cluster:** keep a node's password in a volume across container recreation ([e64284e](https://github.com/ExaDev/github-runner/commit/e64284eed1804c8cc64d8e54d836415659ad8bd0))
* **cluster:** recreate servers one at a time and only while the others are Ready ([c2f356d](https://github.com/ExaDev/github-runner/commit/c2f356d854229f506f0ee3b6812a68e02e4e2522))

### Bug Fixes

* **cluster:** wait for the recreated server heartbeat and relax the check before anything joined ([46cf254](https://github.com/ExaDev/github-runner/commit/46cf25437295cfa28b458059965d886af891b2b5))

### Documentation

* **cluster:** describe the heartbeat wait and the bring-up exception ([d1d7fbe](https://github.com/ExaDev/github-runner/commit/d1d7fbedb26f708b793961d7f066d2e4535ab7d8))
* **cluster:** document the node password volume and the servers' turns ([1c5a445](https://github.com/ExaDev/github-runner/commit/1c5a4454ad8b0e1583f639c3cc858984f5c9e650))

### Tests

* **cluster:** count a server's turn as its Compose start in the end-check order ([e43c851](https://github.com/ExaDev/github-runner/commit/e43c851470f1adbe4a7788d7168d430c2ef27d7b))
* **mesh:** recreate a joined server and check it keeps its node password ([c1e169d](https://github.com/ExaDev/github-runner/commit/c1e169df7d6b70a7316d0ed743b711da5714c2c5))
* **mesh:** recreate the joined server after the scenario's other steps ([23e3ed9](https://github.com/ExaDev/github-runner/commit/23e3ed97ce7f45301f0519e3a2665a0293cbc5e2))
* **mesh:** reuse the restart test's echo pod helper ([684b763](https://github.com/ExaDev/github-runner/commit/684b763c3e82e01b507ec8e94491630a77b7d714))
* **mesh:** wait for the recreated node's echo pod before checking the mesh ([a060aa7](https://github.com/ExaDev/github-runner/commit/a060aa7d2a3965a7a686e95f26bcc1a59e156f68))

### Continuous Integration

* run the unit tests in shards of their own job ([95d6657](https://github.com/ExaDev/github-runner/commit/95d6657ecfc805b120f9c4dc5c22124f0ab050e6))

## [2.2.0](https://github.com/ExaDev/github-runner/compare/v2.1.0...v2.2.0) (2026-10-20)

### Features

* **cluster:** make etcd's heartbeat and election timeouts configurable ([0811f20](https://github.com/ExaDev/github-runner/commit/0811f20f61bf98eba9dbb50834c2b2fc20d8892d))

### Documentation

* **cluster:** document the etcd timing settings ([2c05e63](https://github.com/ExaDev/github-runner/commit/2c05e6399a727798a2e4a0add6b8eac002caf1b2))

### Tests

* **mesh:** restart an initialised etcd datastore with the timing set ([0b37335](https://github.com/ExaDev/github-runner/commit/0b3733579aa3dbbc07c58c338fb429a77ee83d81))
* **mesh:** wait for the restarted server's pod to restart before probing ([3e0221e](https://github.com/ExaDev/github-runner/commit/3e0221ecdd7851413024f15d8a6fec0872335fb8))

## [2.1.0](https://github.com/ExaDev/github-runner/compare/v2.0.1...v2.1.0) (2026-10-20)

### Features

* **cluster:** derive the container runtime from the Docker context ([a6b4bf8](https://github.com/ExaDev/github-runner/commit/a6b4bf8390926f1a643572a945885a217393c937))
* **cluster:** start Colima at login on macOS hosts and check the runtime ([a9dd023](https://github.com/ExaDev/github-runner/commit/a9dd023ada8a1d53b120734654324c0277797d4a))
* **examples:** add a scheduled workflow that runs fleet-alert.sh off the fleet ([5a28c7f](https://github.com/ExaDev/github-runner/commit/5a28c7f285d4f43b6e13a7acb038c293705f2bc6))
* **heartbeat:** add fleet-alert.sh to announce a fleet that has been down ([9324f94](https://github.com/ExaDev/github-runner/commit/9324f94c68129f956b27f242bb63bdebfd77d980))
* **heartbeat:** publish not-Ready nodes and when each problem began ([36eae9b](https://github.com/ExaDev/github-runner/commit/36eae9bf76fa87d00cc92002af29d7ec7eca2a32))

### Bug Fixes

* **cluster:** read the host's operating system instead of assuming gathered facts ([e7a3d83](https://github.com/ExaDev/github-runner/commit/e7a3d8300727879de54e718db7cc6610e83bb67a))
* **heartbeat:** judge a node Ready by its Ready condition ([276bee6](https://github.com/ExaDev/github-runner/commit/276bee6f3b15d30b1faf715934212958ea9033b7))

### Documentation

* **heartbeat:** document the fleet-down notification and where it must run ([bb35601](https://github.com/ExaDev/github-runner/commit/bb356013818873a50b996eaa98037f9715140f9c))

## [2.0.1](https://github.com/ExaDev/github-runner/compare/v2.0.0...v2.0.1) (2026-10-19)

### Bug Fixes

* **arc:** render helm diff through an upgrade dry run in the role's helm tasks ([2c7cc5e](https://github.com/ExaDev/github-runner/commit/2c7cc5e5581cd75c3485402634edb68f4d2a14e6))

## [2.0.0](https://github.com/ExaDev/github-runner/compare/v1.31.4...v2.0.0) (2026-10-18)

### ⚠ BREAKING CHANGES

* **cluster:** a github_runner_cluster_dir or github_runner_cluster_compose_files
path with a colon in it is refused, since COMPOSE_FILE separates paths with colons.
Move the file or directory to a path without one.
* **cluster:** a cluster that runs a newer k3s than the version the role is
given, such as one built from latest before the pins, is refused until
github_runner_cluster_k3s_version names the tag it runs. A deliberate downgrade,
a cluster being replaced or one that cannot be read needs
-e github_runner_cluster_allow_k3s_version_jump=true for that run.
* **cluster:** latest is no longer accepted for github_runner_cluster_k3s_version
or K3S_VERSION in .env.bootstrap. Set the rancher/k3s tag the cluster runs
(k3s --version in a node's container, with + written as -), once for the whole
group, or leave it unset for the default; set github_runner_cluster_tailscale_version
if the nodes run another Tailscale. The next run rewrites each node's .env and
recreates nothing.

### Features

* **cluster:** name the node's Compose files and project in its .env ([88f7e9c](https://github.com/ExaDev/github-runner/commit/88f7e9cfe31087463bd90cb91cbb401f8948817b))
* **cluster:** pin the k3s and tailscale images every node is built from ([1a37eac](https://github.com/ExaDev/github-runner/commit/1a37eac273b81685b6b7adf214a3c8c3ee0577c4))
* **cluster:** refuse a k3s version outside the upgrade path from what runs ([cc7255c](https://github.com/ExaDev/github-runner/commit/cc7255cb471277914fe8fc1ba946a46c27bf1c88))

### Documentation

* **cluster:** document the version pins, their checks and an upgrade ([d70a5ad](https://github.com/ExaDev/github-runner/commit/d70a5ad7a5315b6fe727689a17ebb7f7f34a1379))

### Tests

* **bootstrap:** check bootstrap.sh leaves unset image versions to the role ([04350ce](https://github.com/ExaDev/github-runner/commit/04350ce6925c8ecd4400a4e3b5cd1239037574b7))
* **renewal:** suspend the renewer's CronJob while the test runs its Jobs ([adca1e1](https://github.com/ExaDev/github-runner/commit/adca1e1fddfe698a9b10c0eb502cfefb3d4f678b))

### Continuous Integration

* run the unit tests' playbooks with the ansible-core their filters import ([381db70](https://github.com/ExaDev/github-runner/commit/381db707b4f2b35a2e7b49ef22c51966fbc3a431))

## [1.31.4](https://github.com/ExaDev/github-runner/compare/v1.31.3...v1.31.4) (2026-10-18)

### Documentation

* **github-runner-arc:** name the pnpm store with a variable pnpm does not read ([96afd9c](https://github.com/ExaDev/github-runner/commit/96afd9ccc818df4c9d90c4192e3ed4bc6388e971))
* **github-runner-arc:** read the pnpm store variable in the install step's shell ([74970f4](https://github.com/ExaDev/github-runner/commit/74970f4dfb0d737920e760e68930837684e6b7a6))

## [1.31.3](https://github.com/ExaDev/github-runner/compare/v1.31.2...v1.31.3) (2026-10-15)

### Documentation

* **github-runner-arc:** drop a private repository reference from the burst-node note ([c2f4400](https://github.com/ExaDev/github-runner/commit/c2f440053170652671b359a4432a06edc73070f3))

## [1.31.2](https://github.com/ExaDev/github-runner/compare/v1.31.1...v1.31.2) (2026-10-15)

### Bug Fixes

* **arc:** drop FOWNER from the node-cache init container and document its capabilities ([e641d8f](https://github.com/ExaDev/github-runner/commit/e641d8ff2953ec8f419a3760d5d890a5011a9752))
* **arc:** keep DAC_OVERRIDE and FOWNER on the node-cache init container ([320c3e5](https://github.com/ExaDev/github-runner/commit/320c3e532764b960c7cf4d171c3f1326ce31e494))

### Tests

* **arc:** fail the node-cache step on any assertion and explain the single init container ([7f718fd](https://github.com/ExaDev/github-runner/commit/7f718fd1cc958b0a06c220f4f2f564bde2def4a1))
* **arc:** print the seeded pod's end marker from the step ([1778f86](https://github.com/ExaDev/github-runner/commit/1778f8692cecdff4d2273855a8acf4ba06e7ef5e))
* **arc:** run a node-cache capability's init container in kind under a non-root pod context ([b8c69ce](https://github.com/ExaDev/github-runner/commit/b8c69cedd4e989b206b6cd9e84a4d548b4ff9afa))
* **arc:** show every container's log when the capabilities test pod fails before its step ([7db8a42](https://github.com/ExaDev/github-runner/commit/7db8a42cc61205bcdde11ba91bf32ed80f1dd471))
* **arc:** start a node-cache pod on a directory the runner user already owns ([07f7ad6](https://github.com/ExaDev/github-runner/commit/07f7ad6303b35614e58381f33d4ab45373c7a318))

## [1.31.1](https://github.com/ExaDev/github-runner/compare/v1.31.0...v1.31.1) (2026-10-15)

### Bug Fixes

* **arc:** bound the pruner's numeric settings so its arithmetic cannot wrap, in the role too ([e7d4e1f](https://github.com/ExaDev/github-runner/commit/e7d4e1f445ae25cd49cd9a4236c1575f892cc27d))
* **arc:** build node cache paths from one canonical root and report what the pruner could not do ([d44aba0](https://github.com/ExaDev/github-runner/commit/d44aba02388a36f79459257d1a47caf8b7cde63d))
* **arc:** empty a node cache atomically, drain a node past its hard limit, and narrow the pruner ([886b4b4](https://github.com/ExaDev/github-runner/commit/886b4b4125df68685cb08c02db9a95ef59ae6aad))
* **arc:** keep a departed profile's pod access while the pruner runs, and match only the role's own ([7439708](https://github.com/ExaDev/github-runner/commit/74397080b149b2d8123010f819a09de170969329))
* **arc:** refuse a pruner start with an invalid limit, and name the cause when a final list fails ([8d53e99](https://github.com/ExaDev/github-runner/commit/8d53e9931b5c1fc3bda7b597f53d2422aa990e72))
* **arc:** remove only the cluster role 1.31.0 made, report why rmdir failed, and guard empty arrays ([ad1edc3](https://github.com/ExaDev/github-runner/commit/ad1edc31b6383bd324910219b70f4bbb16bf5fbd))
* **arc:** report a busy node cache over its hard limit instead of cordoning, and gate each rename ([0c4e3fc](https://github.com/ExaDev/github-runner/commit/0c4e3fca03eba1d6308df8ec20d92c29a0e515ef))
* **arc:** tell a refused pod list from a failed one, and build images from the release tag ([6ad1ea4](https://github.com/ExaDev/github-runner/commit/6ad1ea4235101e7305b8d6a388ad9530fe8b6fc5))
* **arc:** treat the 1.31.0 cluster role and binding together, and say what the cleanup needs ([949ebb2](https://github.com/ExaDev/github-runner/commit/949ebb2fb09cd7e2b97b1e4d01c458b7a009f201))

### Documentation

* **arc:** reword the 1.31.0 cleanup note, and tidy the new tests' style findings ([394d74b](https://github.com/ExaDev/github-runner/commit/394d74b8670b31ffc01188dd7aee11317c4e8eab))
* **arc:** say that burst nodes are not pruned, and drop a duplicate comment ([814f9a5](https://github.com/ExaDev/github-runner/commit/814f9a5c5e4511c03c654ac8271d783e8c5f2c7b))

## [1.31.0](https://github.com/ExaDev/github-runner/compare/v1.30.1...v1.31.0) (2026-10-14)

### Features

* **arc:** add a node-cache capability that shares a node directory between a profile's pods ([39c55dc](https://github.com/ExaDev/github-runner/commit/39c55dc0e095d0d0d64c1d6f7918ff12434f402e))

## [1.30.1](https://github.com/ExaDev/github-runner/compare/v1.30.0...v1.30.1) (2026-10-14)

### Bug Fixes

* **arc:** leave the runners a pod capacity bounds out of the safety margin ([ce759b1](https://github.com/ExaDev/github-runner/commit/ce759b178eaa92df6f018d642d045129fcd0cf6b))

## [1.30.0](https://github.com/ExaDev/github-runner/compare/v1.29.0...v1.30.0) (2026-10-14)

### Features

* **arc:** bound each node's measured runners by its pod capacity ([ccf41b4](https://github.com/ExaDev/github-runner/commit/ccf41b40110cdf0352d08a3b065805a2bfba17f2))

## [1.29.0](https://github.com/ExaDev/github-runner/compare/v1.28.0...v1.29.0) (2026-10-14)

### Features

* **autoscaler:** lower maxRunners while a node's own I/O or cpu pressure is high ([2f872ba](https://github.com/ExaDev/github-runner/commit/2f872baf2d058d16faf62052a5457ea432e64f56)), closes [#82](https://github.com/ExaDev/github-runner/issues/82)

## [1.28.0](https://github.com/ExaDev/github-runner/compare/v1.27.0...v1.28.0) (2026-10-14)

### Features

* **arc:** run the controller and listeners under the platform priority class when asked ([c90738f](https://github.com/ExaDev/github-runner/commit/c90738f295e9958fc6edd117bf84de731878f95b)), closes [#84](https://github.com/ExaDev/github-runner/issues/84)

## [1.27.0](https://github.com/ExaDev/github-runner/compare/v1.26.4...v1.27.0) (2026-10-14)

### Features

* **arc:** let a profile name tolerations, and count the nodes they let its pods use ([1bacb38](https://github.com/ExaDev/github-runner/commit/1bacb38c62dcf2dfacacb94c699a8ddad03e8fbe))

## [1.26.4](https://github.com/ExaDev/github-runner/compare/v1.26.3...v1.26.4) (2026-10-13)

### Bug Fixes

* **arc:** force field conflicts when Helm 4 upgrades the controller and scale sets ([1aaa0f8](https://github.com/ExaDev/github-runner/commit/1aaa0f8043174a6e40ede31b9eb7fe6371995c0b))
* **arc:** pass neither server-side option to the helm module on Helm 3 ([6ebef08](https://github.com/ExaDev/github-runner/commit/6ebef080122a2ff0a7eeb1c53757ccbfbda58afc))
* **ci:** install the lowest kubernetes.core even when it is also the newest ([3a424fc](https://github.com/ExaDev/github-runner/commit/3a424fc263fda1ae878fcf605fc13fb5bd4d7541))

### Documentation

* **arc:** say which server-side option the helm module refuses on Helm 3 ([ea6b69a](https://github.com/ExaDev/github-runner/commit/ea6b69a64254507c1863ad97271c51a55de178ab))

### Tests

* **arc:** read the kubernetes.core floor in one place and report a wrong one once ([fb95bf8](https://github.com/ExaDev/github-runner/commit/fb95bf82aae90636ae804632f288aee910379934))
* **arc:** run the Helm apply phases with the lowest accepted kubernetes.core too ([fabca3d](https://github.com/ExaDev/github-runner/commit/fabca3db9facc65f3c12f6835910c2e7131f789e))
* **arc:** run the role's scale-set helm task with Helm 3 and Helm 4 in kind ([3f1058b](https://github.com/ExaDev/github-runner/commit/3f1058bcc4b0aa831f65e92e46b0cca7957a6d1f))

### Continuous Integration

* pin the Helm archive digests the Helm apply workflow checks ([a1ac5a1](https://github.com/ExaDev/github-runner/commit/a1ac5a14fccfa02262541f7d63b5e220653f9b93))

## [1.26.3](https://github.com/ExaDev/github-runner/compare/v1.26.2...v1.26.3) (2026-10-13)

### Bug Fixes

* **autoscaler:** keep kubectl's error when reading node metrics fails ([96b0fb6](https://github.com/ExaDev/github-runner/commit/96b0fb6016159d3c106a431cd75b1a324b831aad))
* **autoscaler:** read memory pressure only from the nodes runner pods may use ([583aa42](https://github.com/ExaDev/github-runner/commit/583aa420d91b8730ace4eb221ea3c505ef8b0312))

### Documentation

* **autoscaler:** describe when kubectl reports the metrics API as unavailable ([afb6813](https://github.com/ExaDev/github-runner/commit/afb68134fce0a3494c2745e4dab8c68caab0294a))
* **autoscaler:** name each error kubectl top nodes can fail with ([da2901a](https://github.com/ExaDev/github-runner/commit/da2901a93e6b87c5e75c52c92c28e9ee4bd7964c))

### Tests

* **autoscaler:** fail safe when a node inside the selector reports no metrics ([145320d](https://github.com/ExaDev/github-runner/commit/145320d04464443b895e4cf45887b35cc0cb85ac))

## [1.26.2](https://github.com/ExaDev/github-runner/compare/v1.26.1...v1.26.2) (2026-10-09)

### Bug Fixes

* **arc:** stop the registry cache install loop resolving the container resources setting ([eb1c865](https://github.com/ExaDev/github-runner/commit/eb1c865a74eaa8bf7341f96e42b2075bf3d284de)), closes [#71](https://github.com/ExaDev/github-runner/issues/71)

## [1.26.1](https://github.com/ExaDev/github-runner/compare/v1.26.0...v1.26.1) (2026-10-08)

### Bug Fixes

* **arc:** keep the sysroot's pkg-config files visible after actions/setup-python ([779c361](https://github.com/ExaDev/github-runner/commit/779c3615d28256eda019b36073f487c70149ad4a))

## [1.26.0](https://github.com/ExaDev/github-runner/compare/v1.25.0...v1.26.0) (2026-10-08)

### Features

* **arc:** add a runner reaper pass for runners that outlive their job ([f66a9c5](https://github.com/ExaDev/github-runner/commit/f66a9c56d6852a001473e7352c228be351ac3236))
* **arc:** install an optional runner reaper CronJob in each runner namespace ([a60458f](https://github.com/ExaDev/github-runner/commit/a60458f9ce07a219fff16523433166087cbf90a4))
* **arc:** set activeDeadlineSeconds on runner pods when configured ([7964cbd](https://github.com/ExaDev/github-runner/commit/7964cbd2f4fd16332ae8ec6f06d7753f5160c449))

### Code Refactoring

* **arc:** share the App installation token minting between jobs ([58e29be](https://github.com/ExaDev/github-runner/commit/58e29be445197310099fe270283685afc83fc079))
* **arc:** word a refused token mint for any permission ([77cecc7](https://github.com/ExaDev/github-runner/commit/77cecc76332a43883db2b23ab8f9dd1a129ae49b))

### Documentation

* **arc:** document the runner reaper, its bound, permission and safety ([2578475](https://github.com/ExaDev/github-runner/commit/2578475bcf43a85c9e6a365def9e86c372163229))

### Tests

* **arc:** check the reaper reads only the assigned runners' runs by run id ([c9d7f3b](https://github.com/ExaDev/github-runner/commit/c9d7f3b0f838002f202530a91f4f75be034a2a46))
* **arc:** show the runner reaper deleting stuck runners in kind ([8b37eab](https://github.com/ExaDev/github-runner/commit/8b37eab3d4ae194c06d0f3a7a6ad204595a51c37))

## [1.25.0](https://github.com/ExaDev/github-runner/compare/v1.24.0...v1.25.0) (2026-10-08)

### Features

* **arc:** add an optional pull-through registry cache for dind runners ([223786b](https://github.com/ExaDev/github-runner/commit/223786b0b73bd0dae3ddb6179a786c41bf2a8cc7))

### Bug Fixes

* **arc:** let the registry cache answer referrers lookups ([3d1dc84](https://github.com/ExaDev/github-runner/commit/3d1dc849fc78e7ea3c783d04ca1caff837a8a9a3))

### Documentation

* **arc:** document the registry cache, its pull paths and its security ([670dd87](https://github.com/ExaDev/github-runner/commit/670dd8721decebafa0d7c4aeaf3475bb486347ac))

### Tests

* **arc:** make the registry cache test independent of the host's Docker ([62c3cfc](https://github.com/ExaDev/github-runner/commit/62c3cfc7addc2dcdab5fcfb9c4083ef7649378a2))
* **arc:** prove pulls go through the registry cache and fail open in kind ([0bb0dcf](https://github.com/ExaDev/github-runner/commit/0bb0dcf2ae77b93337ce563a28c4192b518cc692))
* **capabilities:** ignore Helm 4's pull notice when reading the rendered chart ([159ae13](https://github.com/ExaDev/github-runner/commit/159ae13c954e3d7d2eedc86e3e89f364b4c55c9f))

### Continuous Integration

* run the registry cache integration test on pull requests ([7553f15](https://github.com/ExaDev/github-runner/commit/7553f15e34aeb41c7c27452c6cdcb9e99ca20b75))

## [1.24.0](https://github.com/ExaDev/github-runner/compare/v1.23.0...v1.24.0) (2026-10-08)

### Features

* **tool-images:** add a build runner image with a C toolchain ([6df87bf](https://github.com/ExaDev/github-runner/commit/6df87bff8cd24897c41b93c7e6ce2ed952f5cd65))
* **tool-images:** add GLPK and MariaDB Connector/C system-library images ([3d9e6b6](https://github.com/ExaDev/github-runner/commit/3d9e6b6c87250a3affa58b9136da4ac3aa7c1131))

## [1.23.0](https://github.com/ExaDev/github-runner/compare/v1.22.1...v1.23.0) (2026-10-08)

### Features

* **arc:** run public images without a pull Secret ([f5fe08f](https://github.com/ExaDev/github-runner/commit/f5fe08f2c43d579e5f866710a4a17aed5e0679a9))

## [1.22.1](https://github.com/ExaDev/github-runner/compare/v1.22.0...v1.22.1) (2026-10-08)

### Documentation

* **arc:** pin the capability example to the published tool images ([d00c919](https://github.com/ExaDev/github-runner/commit/d00c919a2f3c74179f6d674b173c264eb7428c96))
* drop the note that no release has run yet ([8245c61](https://github.com/ExaDev/github-runner/commit/8245c61a8feb9b2cfc59d973e96188463d0580e9))

## [1.22.0](https://github.com/ExaDev/github-runner/compare/v1.21.0...v1.22.0) (2026-10-07)

### Features

* **cluster:** register a node with taints set per host ([85171e6](https://github.com/ExaDev/github-runner/commit/85171e6ff1472f36df6235dc87b493450f90503e))

## [1.21.0](https://github.com/ExaDev/github-runner/compare/v1.20.0...v1.21.0) (2026-10-07)

### Features

* install a standalone Python on hosts that have none ([fb1b5d0](https://github.com/ExaDev/github-runner/commit/fb1b5d093c49296a5d5f8673133718011e141fa8))

## [1.20.0](https://github.com/ExaDev/github-runner/compare/v1.19.0...v1.20.0) (2026-10-07)

### Features

* **autoscaler:** move a slot to a saturated pooled profile when the pool is at its ceiling ([0e75801](https://github.com/ExaDev/github-runner/commit/0e75801c4f3ec7f926ea551a254278f00cf8dcaa))

### Bug Fixes

* **autoscaler:** count runners from ephemeral runners and patch the state ConfigMap from a file ([0da00e9](https://github.com/ExaDev/github-runner/commit/0da00e919a6a8e9f15330f87d2b85bdae5b9b9dd))

## [1.19.0](https://github.com/ExaDev/github-runner/compare/v1.18.0...v1.19.0) (2026-10-07)

### Features

* **arc:** add system-library capabilities ([fab2a78](https://github.com/ExaDev/github-runner/commit/fab2a78141a32d030b94d43b28e7470aaf8d1db3))
* **tool-images:** add reference system-library payload images ([4b2b973](https://github.com/ExaDev/github-runner/commit/4b2b9730a862290249dcc508edf7d4e9f3f48ba5))

### Documentation

* describe system-library capabilities and the payload contract ([7111ffa](https://github.com/ExaDev/github-runner/commit/7111ffafb24a24f7380cf1c26b744bb9ba898684))

### Tests

* **capabilities:** carry a system-library capability through kind ([d0ae25a](https://github.com/ExaDev/github-runner/commit/d0ae25ad763c7fbb40460931d7f3b88acec80227))
* **capabilities:** prove each system-library payload works relocated ([5abdb4e](https://github.com/ExaDev/github-runner/commit/5abdb4eed17dca4bf34e922e864cfb62f0f4b364))

### Continuous Integration

* add a workflow that publishes the reference tool images ([e190279](https://github.com/ExaDev/github-runner/commit/e19027938b3b88a33f651ab94ec21acdd2941c38))
* **publish-tool-images:** check the pinned version with an if rather than && and || ([05d233a](https://github.com/ExaDev/github-runner/commit/05d233a81b6416597997a9e183579bd669ed78e7))

## [1.18.0](https://github.com/ExaDev/github-runner/compare/v1.17.2...v1.18.0) (2026-10-07)

### Features

* **arc:** add capabilities to scale-set profiles ([e0e6ce9](https://github.com/ExaDev/github-runner/commit/e0e6ce9817d9ed39cb0174879ffddb080d2411ca)), closes [#55](https://github.com/ExaDev/github-runner/issues/55)
* **tool-images:** add reference tool-cache capability images ([f33107f](https://github.com/ExaDev/github-runner/commit/f33107fa7693b3040bff3d9b30db06785e46ab1f)), closes [#55](https://github.com/ExaDev/github-runner/issues/55)

### Documentation

* describe runner capabilities and the tool-image contract ([f8421a6](https://github.com/ExaDev/github-runner/commit/f8421a6b5adf0b45b64d0f8d0d0bb557f6ac28dc)), closes [#55](https://github.com/ExaDev/github-runner/issues/55)

### Tests

* **capabilities:** run a capability end to end in kind ([0232461](https://github.com/ExaDev/github-runner/commit/023246197357c766f86a3949b08081f49a3d2495)), closes [#55](https://github.com/ExaDev/github-runner/issues/55)

## [1.17.2](https://github.com/ExaDev/github-runner/compare/v1.17.1...v1.17.2) (2026-10-07)

### Documentation

* document the br_netfilter requirement after a Colima VM restart ([09471b0](https://github.com/ExaDev/github-runner/commit/09471b010d3d1ef50653ebf2c1227528827b2553))

## [1.17.1](https://github.com/ExaDev/github-runner/compare/v1.17.0...v1.17.1) (2026-10-04)

### Bug Fixes

* **release:** commit the cluster role's stamped version with the release ([1f335cd](https://github.com/ExaDev/github-runner/commit/1f335cd65ab9cb90a71e085a1264a67f8eba4614))

## [1.17.0](https://github.com/ExaDev/github-runner/compare/v1.16.0...v1.17.0) (2026-10-04)

### Features

* **cluster:** let a host without API access skip the node version stamp ([5d3829b](https://github.com/ExaDev/github-runner/commit/5d3829b6f5ebccf842aef3e20fb01dfc54064541))
* publish each scope's deployed collection version in fleet-health.json ([485ea54](https://github.com/ExaDev/github-runner/commit/485ea54901dfd2fdb0e4eccea800858cf8bbdcf1))

### Bug Fixes

* **arc:** restore the collection version and deployed-state defaults the release stamps ([beeba32](https://github.com/ExaDev/github-runner/commit/beeba32e965f77d1adfc6dea80d6142f6f2b664f))

### Documentation

* name the deployed-state ConfigMap in the heartbeat's RBAC summary ([7427102](https://github.com/ExaDev/github-runner/commit/742710280dfc2ea98982fd6e31ebca1fd817990f))

## [1.16.0](https://github.com/ExaDev/github-runner/compare/v1.15.0...v1.16.0) (2026-10-04)

### Features

* **heartbeat:** show how broad and how long the measured peaks are ([a9d8076](https://github.com/ExaDev/github-runner/commit/a9d8076ae8a253d44a58780a73797360ee0f6ad8))

## [1.15.0](https://github.com/ExaDev/github-runner/compare/v1.14.0...v1.15.0) (2026-10-03)

### Features

* **heartbeat:** publish the distribution of measured job peak memory ([9434c80](https://github.com/ExaDev/github-runner/commit/9434c8088a79842c0ae409fad21ae63501ac30be))

## [1.14.0](https://github.com/ExaDev/github-runner/compare/v1.13.0...v1.14.0) (2026-10-03)

### Features

* **arc:** let runner pods create events so jobs can record their peak memory ([8fcfe9d](https://github.com/ExaDev/github-runner/commit/8fcfe9d81987ac240685dfd3dde104f809b8754a))
* **autoscaler:** collect the peaks the runners measured ([b72bf02](https://github.com/ExaDev/github-runner/commit/b72bf0263117b5acacc4671eb7cf920d142aebb5))
* **runner:** record each job's peak memory from a job-completed hook ([850bbd8](https://github.com/ExaDev/github-runner/commit/850bbd8acb0ba2cfdc07776b90e872ad3c829256))

## [1.13.0](https://github.com/ExaDev/github-runner/compare/v1.12.0...v1.13.0) (2026-10-03)

### Features

* **autoscaler:** record each job's observed peak memory ([77ac920](https://github.com/ExaDev/github-runner/commit/77ac920e544d8b9872847afd87d1cfa7b180abe8))

## [1.12.0](https://github.com/ExaDev/github-runner/compare/v1.11.1...v1.12.0) (2026-10-03)

### Features

* **arc:** refuse a controller version change while runners are running ([eeb7259](https://github.com/ExaDev/github-runner/commit/eeb7259e45b218581fab70faf69f9cd8c2f19961))

## [1.11.1](https://github.com/ExaDev/github-runner/compare/v1.11.0...v1.11.1) (2026-10-03)

### Documentation

* **arc:** record how to upgrade ARC on a live fleet ([ca76881](https://github.com/ExaDev/github-runner/commit/ca76881b2cba3ec1cde5c05b8c22283f2c4fe104))

## [1.11.0](https://github.com/ExaDev/github-runner/compare/v1.10.4...v1.11.0) (2026-10-03)

### Features

* **arc:** apply the controller chart's CRDs before installing or upgrading it ([78ae73c](https://github.com/ExaDev/github-runner/commit/78ae73c57d9ca91276d4f8f16f6c5a17ca40ecce))

### Tests

* **arc:** keep helm's warnings out of the manifests sent to the API server ([3d9bdd7](https://github.com/ExaDev/github-runner/commit/3d9bdd7e177be834d6794efd9e988a24f7bc49b4))
* **arc:** name the controller service account when rendering the scale-set chart ([4f768c5](https://github.com/ExaDev/github-runner/commit/4f768c5739a5dca3196f31ec380814b2d36b2576))

## [1.10.4](https://github.com/ExaDev/github-runner/compare/v1.10.3...v1.10.4) (2026-10-03)

### Bug Fixes

* **autoscaler:** lower a pooled total that is above the ceiling ([473e733](https://github.com/ExaDev/github-runner/commit/473e733c5145296b04338dca6082fc155f83aa7f))

## [1.10.3](https://github.com/ExaDev/github-runner/compare/v1.10.2...v1.10.3) (2026-10-03)

### Bug Fixes

* **image:** install python3-venv so actions that build a virtualenv can run ([4cb7352](https://github.com/ExaDev/github-runner/commit/4cb73522ea7ea65b25c2b6cff71d8fa1c8747efd))

## [1.10.2](https://github.com/ExaDev/github-runner/compare/v1.10.1...v1.10.2) (2026-10-03)

### Bug Fixes

* **autoscaler:** read an absent currentRunners as zero running runners ([6149ed2](https://github.com/ExaDev/github-runner/commit/6149ed2f9dbddac9bb65a1fc1d55df137071115a))

## [1.10.1](https://github.com/ExaDev/github-runner/compare/v1.10.0...v1.10.1) (2026-10-03)

### Bug Fixes

* **heartbeat:** stop vouching for the fleet when a scale set or its listener is missing ([6bbe74f](https://github.com/ExaDev/github-runner/commit/6bbe74f79d74a03da607f9557147e59e8b2f0130))

### Tests

* **heartbeat:** assert what each tick writes to the gist ([64dda97](https://github.com/ExaDev/github-runner/commit/64dda9781376c58bbe11366f29f8a75e17a42562))

## [1.10.0](https://github.com/ExaDev/github-runner/compare/v1.9.0...v1.10.0) (2026-10-03)

### Features

* **cluster:** prune stale build cache and dangling images on every host ([e683de7](https://github.com/ExaDev/github-runner/commit/e683de7d83b2eeeb2942b8e922885912de6a05a4))

### Bug Fixes

* **cluster:** leave the docker-prune container unnamed so projects on one daemon do not collide ([1a7133e](https://github.com/ExaDev/github-runner/commit/1a7133ef3c69a59a208559acf643acdddec1643e))

## [1.9.0](https://github.com/ExaDev/github-runner/compare/v1.8.1...v1.9.0) (2026-10-03)

### Features

* **heartbeat:** publish why the fleet is unhealthy and which nodes are under pressure ([56cb613](https://github.com/ExaDev/github-runner/commit/56cb6138d5d36900355f659d61490675cb3d625f))
* **platform:** run the platform pods above runner pods with a local-storage request ([d6d4a69](https://github.com/ExaDev/github-runner/commit/d6d4a69a3f20e57e1ac03f3b8d2eea00abbb3af1))

### Bug Fixes

* **platform:** render the priority class as a template the node recovery test also applies ([bcbdab7](https://github.com/ExaDev/github-runner/commit/bcbdab7f780b33e1157cdd4be694238c41de36a8))

### Tests

* **platform:** cover the autoscaler and heartbeat decisions against a scripted kubectl ([f697649](https://github.com/ExaDev/github-runner/commit/f697649a401664f746d5e8ea756ba9815b3f4196))

## [1.8.1](https://github.com/ExaDev/github-runner/compare/v1.8.0...v1.8.1) (2026-10-03)

### Build System

* **hooks:** run commitlint on every commit message locally ([1682bab](https://github.com/ExaDev/github-runner/commit/1682babcb853e3303f16cfc1b181ad21a38e61fb))

## [1.8.0](https://github.com/ExaDev/github-runner/compare/v1.7.0...v1.8.0) (2026-10-03)

### Features

* **heartbeat:** stop vouching for the fleet while runner pods cannot be scheduled ([f875714](https://github.com/ExaDev/github-runner/commit/f875714c016bac0a01fced9b1983b2b818457903))

### Bug Fixes

* **autoscaler:** read every target before failing safe, lower on unschedulable runner pods ([9c3a824](https://github.com/ExaDev/github-runner/commit/9c3a8243a576f2763e163619a6026ab2f64a7ae9))

## [1.7.0](https://github.com/ExaDev/github-runner/compare/v1.6.0...v1.7.0) (2026-09-30)

### Features

* **arc:** let a profile run on burst nodes only ([44de91b](https://github.com/ExaDev/github-runner/commit/44de91b6d5cec772cc4efe21be45d25417370cd2))

## [1.6.0](https://github.com/ExaDev/github-runner/compare/v1.5.2...v1.6.0) (2026-09-30)

### Features

* **arc:** let a sized profile's runner pods overflow onto burst nodes ([94a0b2b](https://github.com/ExaDev/github-runner/commit/94a0b2bfc08c1604f6b9c324f3ef6480b794f7a9))

## [1.5.2](https://github.com/ExaDev/github-runner/compare/v1.5.1...v1.5.2) (2026-09-28)

### Bug Fixes

* register gcc-11/g++-11 as the generic gcc/g++/cc/c++ alternatives ([ebfd896](https://github.com/ExaDev/github-runner/commit/ebfd8964633356943fdbb18604f2b2875b8a570a))

## [1.5.1](https://github.com/ExaDev/github-runner/compare/v1.5.0...v1.5.1) (2026-09-28)

### Bug Fixes

* install make in the runner image for native addon builds ([e53aad6](https://github.com/ExaDev/github-runner/commit/e53aad62e588446332b050b55b1e8783033ded7f))

## [1.5.0](https://github.com/ExaDev/github-runner/compare/v1.4.0...v1.5.0) (2026-09-28)

### Features

* **arc:** pool multiple autoscaled profiles into one shared budget ([82eddac](https://github.com/ExaDev/github-runner/commit/82eddacd5891c01bf15cb88ae1e6fa10ae11e624))

### Bug Fixes

* **arc:** drop unnecessary $/${} on arithmetic array subscripts ([78be6df](https://github.com/ExaDev/github-runner/commit/78be6dfdcecf211cda61c0b64d99d3207b646423))

## [1.4.0](https://github.com/ExaDev/github-runner/compare/v1.3.1...v1.4.0) (2026-09-26)

### Features

* **cluster:** pin the Docker context the role drives per host ([63bb283](https://github.com/ExaDev/github-runner/commit/63bb283677690b213d1b17c1a0c046265ee2272c))

### Tests

* **cluster:** prove a pinned Docker context wins over the current one ([794a6a6](https://github.com/ExaDev/github-runner/commit/794a6a60508c75cbaaa27b0dcb4b6cbcdcd7c1af))
* **cluster:** return the playbook status from the mesh harness run_role ([eab77e0](https://github.com/ExaDev/github-runner/commit/eab77e06d32a6462765759a69224b6bd8c2d1d63))

## [1.3.1](https://github.com/ExaDev/github-runner/compare/v1.3.0...v1.3.1) (2026-09-26)

### Bug Fixes

* **cluster:** refuse Docker Compose 2.37.1 to 2.38.x ([b99ae54](https://github.com/ExaDev/github-runner/commit/b99ae54e1576eab76b6d67cd202f126c83d81149))

### Tests

* **mesh:** cover both sides of the faulty Compose range ([224dfac](https://github.com/ExaDev/github-runner/commit/224dfacdbec4c39c87bbf3c7c8b2f1248ee5dedf))

## [1.3.0](https://github.com/ExaDev/github-runner/compare/v1.2.2...v1.3.0) (2026-09-26)

### Features

* **arc:** taint a stopped node out of service so its pods are replaced ([8d6d44b](https://github.com/ExaDev/github-runner/commit/8d6d44b8bb4b78f388f02aed05a8c52772f52abb)), closes [#2](https://github.com/ExaDev/github-runner/issues/2)

### Tests

* **arc:** prove node recovery by stopping a worker in a kind cluster ([6476a2f](https://github.com/ExaDev/github-runner/commit/6476a2fe1e5565ab252e9e9b843551a290ead7b6))

## [1.2.2](https://github.com/ExaDev/github-runner/compare/v1.2.1...v1.2.2) (2026-09-26)

### Bug Fixes

* **arc:** read the App's id from an existing App Secret ([e0854d2](https://github.com/ExaDev/github-runner/commit/e0854d24506d26650e3197d6d7f425ca1814f36e))

## [1.2.1](https://github.com/ExaDev/github-runner/compare/v1.2.0...v1.2.1) (2026-09-26)

### Bug Fixes

* **arc:** count this host's orgs however they were supplied ([c731751](https://github.com/ExaDev/github-runner/commit/c7317512d4c47098b1342fd507df9d072470d156))

## [1.2.0](https://github.com/ExaDev/github-runner/compare/v1.1.0...v1.2.0) (2026-09-26)

### Features

* **arc:** let the App setup write the App Secret and remove the key ([7f64f21](https://github.com/ExaDev/github-runner/commit/7f64f21bea9d9f2a3689b1be26d7a27122fa6054))
* **arc:** measure the runner ceiling from each node's free capacity ([a9a498b](https://github.com/ExaDev/github-runner/commit/a9a498b7c03b5cd1741b0b71cf53cd93cf96f1dc))
* **arc:** mint the image pull secret from the runners' GitHub App ([6d1c8a3](https://github.com/ExaDev/github-runner/commit/6d1c8a3c17555cf5b9d57608d896c59af74335f6))

### Bug Fixes

* **arc:** keep each org's resolved installation id to that org ([6c1b65a](https://github.com/ExaDev/github-runner/commit/6c1b65a1b0026241d57b1a186c0bee5b80f1973b))

### Tests

* **arc:** run the pull secret renewal CronJob in a kind cluster ([45807a9](https://github.com/ExaDev/github-runner/commit/45807a92304bd71e5b1af70834ce864a7ffde172))

## [1.1.0](https://github.com/ExaDev/github-runner/compare/v1.0.3...v1.1.0) (2026-09-25)

### Features

* **arc:** describe the role's variables in an argument spec ([e565cdb](https://github.com/ExaDev/github-runner/commit/e565cdbdf3c33c601ee01f86f1e22c6ab80f707d))
* **arc:** let orgs and scale-set profiles override their names and labels ([5d2a9b6](https://github.com/ExaDev/github-runner/commit/5d2a9b6f80a43a761ec8052c92a72829f07b1891))
* **arc:** make the controller's release name and namespace configurable ([619ac9e](https://github.com/ExaDev/github-runner/commit/619ac9e4cac89a0347d2ef2ac74976c9f1b29323))
* **arc:** use an existing image pull Secret without managing it ([4d2efe6](https://github.com/ExaDev/github-runner/commit/4d2efe61bccb2b4cbd46214e61a18e7aad972564))

## [1.0.3](https://github.com/ExaDev/github-runner/compare/v1.0.2...v1.0.3) (2026-09-25)

### Bug Fixes

* **cluster:** never delete a server's Node object when clearing a stale registration ([826b66e](https://github.com/ExaDev/github-runner/commit/826b66e4ba1302bc36f5b64be4b0b81f9c72d260))

## [1.0.2](https://github.com/ExaDev/github-runner/compare/v1.0.1...v1.0.2) (2026-09-25)

### Bug Fixes

* **cluster:** clear only the registrations of nodes that have not rejoined ([e789205](https://github.com/ExaDev/github-runner/commit/e7892053e46dac025df0afa5dca00e14d9f05c68))

## [1.0.1](https://github.com/ExaDev/github-runner/compare/v1.0.0...v1.0.1) (2026-09-25)

### Bug Fixes

* **release:** attach the collection tarball to the GitHub release ([32cafd5](https://github.com/ExaDev/github-runner/commit/32cafd5055ef4220ff6f7e6c4d933591cf5a920d))

## 1.0.0 (2026-09-25)

### ⚠ BREAKING CHANGES

* is major, feat is minor, every other conventional type
(fix, perf, refactor, docs, and so on) is patch, matching
commitlint.config.js's own accepted types one for one.

Its exec step runs scripts/release/stamp_version.py, which writes the
new version into galaxy.yml's version field and into the arc role's
two published-image tag defaults (github_runner_arc_heartbeat_image,
github_runner_arc_autoscaler_image), matched by variable name rather
than line number so it stays correct against concurrent edits to that
file, then builds the collection tarball. The git step commits the
changelog and both stamped files back to main with a skip-ci release
commit; the github step attaches the tarball to the GitHub Release;
the exec publish step pushes to Ansible Galaxy only when a
GALAXY_API_KEY is actually configured.
* .env.bootstrap's EXADEV_APP_ID, EXADEV_APP_INSTALLATION_ID and EXADEV_APP_PRIVATE_KEY_PATH are now RUNNER_APP_ID, RUNNER_APP_INSTALLATION_ID and RUNNER_APP_PRIVATE_KEY_PATH, alongside the new RUNNER_ORG, RUNNER_IMAGE and RUNNER_VALUES_FILE or RUNNER_MAX_RUNNERS, and the autoscaler's budget, ceiling and floor no longer default to ExaDev's pool.
* **cluster:** github_runner_cluster_repo_root is replaced by github_runner_cluster_dir, with no alias. A host that ran Compose in a checkout keeps its cluster by setting github_runner_cluster_dir to that checkout, and, if the checkout's directory is not called github-runner, github_runner_cluster_compose_project to that directory's name.
* the github_runner_secrets role is renamed to
github_runner_secrets_onepassword and runs only with
github_runner_secrets_source: onepassword. Its variables are renamed
(github_runner_secrets_vault, _item and _cache_path become
github_runner_secrets_onepassword_vault, _item and _cache_path), and
github_runner_secrets_provided is gone: set the cluster and ARC roles'
variables directly.
* **cluster:** github_runner_cluster_create_tailscale_join_key and
github_runner_cluster_create_headscale_join_key are replaced by
github_runner_cluster_join_key_path and
github_runner_cluster_join_key_listener.
* **arc:** github_runner_arc_org_private_keys is gone. Put each
org's PEM key in its private_key field instead, and drop
org_private_keys from github_runner_secrets_provided.
* github_runner_arc_values_dir is a control-node path with
no default; the autoscaler is installed only for a profile that sets
autoscale: true; github_runner_arc_autoscaler_usable_budget_gi,
_max_ceiling and _floor have no defaults; and the autoscaler image needs
AUTOSCALER_NAMESPACE, AUTOSCALER_RELEASE_NAME and the pool figures set.
* choose k3s servers and agents from the inventory
* split cluster, ARC, secrets and k8s client roles
* move roles and playbook to an Ansible collection layout

### Features

* accept secrets supplied directly as an alternative to 1Password ([8ae7572](https://github.com/ExaDev/github-runner/commit/8ae75726e6210a9a3ea93ccc15c37dc84667b990))
* add a hosted-Headscale mesh provider ([ee82844](https://github.com/ExaDev/github-runner/commit/ee82844acdaf8c5d0e7f3ab231385ff7664f3b0d))
* add a manual workflow to generate load for the autoscaler ([c490889](https://github.com/ExaDev/github-runner/commit/c490889deb38491dc0a7145d1a7f89e7e3d23c14))
* add a playbook that promotes the Headscale warm standby ([15f8878](https://github.com/ExaDev/github-runner/commit/15f8878d18bfb2245ec988390bd30a18acf18543))
* add an ARC-only playbook for an existing cluster ([5c74e03](https://github.com/ExaDev/github-runner/commit/5c74e03025298f8af6caf26eba0448a2a2f44c91))
* add an existing-Headscale mesh provider ([444099c](https://github.com/ExaDev/github-runner/commit/444099ccbe7587c605bbab2634588c38aeadf767))
* add an in-cluster Headscale mesh provider ([2eeaed0](https://github.com/ExaDev/github-runner/commit/2eeaed064583ad822ee5b409fa6409f739079144))
* add ARC metrics and listener readiness and liveness probes ([695e5f1](https://github.com/ExaDev/github-runner/commit/695e5f1364147bc92993972282141867da2c06c5))
* add CI with a Required Checks junction job ([41db343](https://github.com/ExaDev/github-runner/commit/41db3434b199d5a0facc0225efa0635fae869e25)), closes [ExaDev/github-runner#2](https://github.com/ExaDev/github-runner/issues/2)
* add Docker CLI and buildx plugin to the runner image ([0ebce9e](https://github.com/ExaDev/github-runner/commit/0ebce9eec1ebb97e685ead17badbfae129314719))
* add filters that expand ARC org profiles, derive a runner ceiling and split image references ([d2bd850](https://github.com/ExaDev/github-runner/commit/d2bd850b870193467de630c60c48e2e48cfa7261))
* add heartbeat health check for runner-fallback-action ([d7d1091](https://github.com/ExaDev/github-runner/commit/d7d10912f9501fc7fa12ea72507c42548e173761))
* add k3s agent service and TLS SAN flag to compose ([1280a62](https://github.com/ExaDev/github-runner/commit/1280a62a4ed2868a57ddcd95df02c1d7f3bd1811))
* add self-contained Ansible inventory and playbook ([d8953db](https://github.com/ExaDev/github-runner/commit/d8953db16e48aa2e19b6d11b8e9b6bd27e3f907f))
* add the autoscaler container image and poll loop ([ecfac73](https://github.com/ExaDev/github-runner/commit/ecfac732c77d16233124b80a8b76bcc162046f8e))
* add the usage-driven maxRunners autoscaler script ([8eb8fd8](https://github.com/ExaDev/github-runner/commit/8eb8fd8788198b9b9a7af69334b00bd6b5330013))
* allow a single-server cluster on SQLite ([88307c9](https://github.com/ExaDev/github-runner/commit/88307c91bd4ec58a13bb453eb06f02d9cd27eeff))
* **ansible:** add primary/agent node role split to github_runner_arc ([934b9d5](https://github.com/ExaDev/github-runner/commit/934b9d514a139453a23c0d84d22117e294b5db83))
* **ansible:** cache the 1Password secrets read on the control node ([c517d9f](https://github.com/ExaDev/github-runner/commit/c517d9f36ec9a151f24e934eeb22fd5c07b8446a))
* **ansible:** give each node its own node-affinitized scale-set profile ([9ea16d0](https://github.com/ExaDev/github-runner/commit/9ea16d08ab4836f0ddc34fb8bdfe43faeed279ed))
* **ansible:** install helm and kubectl via homebrew on primary hosts ([986fedf](https://github.com/ExaDev/github-runner/commit/986fedf7bf9ee26c0dfab3716856a3654048f80e))
* **ansible:** let a scale-set profile opt out of its org's shared label ([b9dcb0f](https://github.com/ExaDev/github-runner/commit/b9dcb0fb5361cf8d7094ae62fbb05083385ffa6b))
* **ansible:** provision the Tailscale ACL/join key and self-heal two stale-state failure modes ([057ebb1](https://github.com/ExaDev/github-runner/commit/057ebb177e7ee4f7b3a2a14c57f470e08b0c7711))
* **ansible:** self-heal a scale-set listener left pointing at a deleted EphemeralRunnerSet ([48275e1](https://github.com/ExaDev/github-runner/commit/48275e121c64f053ed697798aff3af4da3921357))
* **ansible:** thread K3S_NODE_IP through to every host ([9ad5241](https://github.com/ExaDev/github-runner/commit/9ad5241a6832056d2329082662d5e2492047bd69))
* **ansible:** thread the Tailscale join key through instead of node IPs ([54b035f](https://github.com/ExaDev/github-runner/commit/54b035fe9c6adbb29f70dbcf4e2f186c01385c28))
* **arc:** read each org's App key from its private_key ([ac1b879](https://github.com/ExaDev/github-runner/commit/ac1b879069fdb2f8ee60862879350476d13adcec))
* build and push all three images from the release workflow ([de104de](https://github.com/ExaDev/github-runner/commit/de104de92b20e28fff4e086ec0c75b8ba12ec9b1)), closes [#10](https://github.com/ExaDev/github-runner/issues/10)
* check ARC secrets exist before changing anything, and support pre-created ones ([63d42e7](https://github.com/ExaDev/github-runner/commit/63d42e737a7bbe15915ca5aae6cacc929255a8db))
* choose k3s servers and agents from the inventory ([e2386c2](https://github.com/ExaDev/github-runner/commit/e2386c2e828d62f36cdaee837a3f9a498eb2cc56))
* **ci:** add commitlint, ansible-lint, and a collection build check ([dda2978](https://github.com/ExaDev/github-runner/commit/dda29786c4c5e3d5e008c80723c03a942f2e10c8))
* **cluster:** back up Headscale with Litestream and keep a warm standby ([e538f98](https://github.com/ExaDev/github-runner/commit/e538f987bdf7f155cfff3d041692ba184ce68b5f))
* **cluster:** keep new join keys without calling the secrets role ([3befb78](https://github.com/ExaDev/github-runner/commit/3befb78643164d79c09467c115c127a498035c04))
* configure semantic-release for the collection version ([d190ed6](https://github.com/ExaDev/github-runner/commit/d190ed67b2ed7af1c8b78011005d7dee33bf4d19))
* create the runners' GitHub App through the manifest flow ([9b8412f](https://github.com/ExaDev/github-runner/commit/9b8412f3cea1d66844619706896b4ddbea4aca1d))
* define github_runner_arc role defaults ([3736baf](https://github.com/ExaDev/github-runner/commit/3736bafee071326c9fd523ea8022d143415e6645))
* generalize the primary role into server/agent plus a fleet-health installer ([7aec978](https://github.com/ExaDev/github-runner/commit/7aec97802c140fb1db42c52573882dd55c13adc7))
* genuine 3-node k3s embedded-etcd HA control plane ([72e4b76](https://github.com/ExaDev/github-runner/commit/72e4b7622f4cae9be6d54752b99507260f1a014f))
* heartbeat as a docker-compose service refreshing a secret gist ([9f78ea7](https://github.com/ExaDev/github-runner/commit/9f78ea7244a1571b44808cc7c04920888f415139))
* initial ExaDev self-hosted runner setup ([8665b12](https://github.com/ExaDev/github-runner/commit/8665b12c283478c61d56fec2631d4ad3b80abb4f))
* install one org's namespace, secrets, and runner scale set ([42fdcf8](https://github.com/ExaDev/github-runner/commit/42fdcf897af6ab5cc039616340f81bcd86962fcc))
* let nodes register with a control server other than Tailscale's ([140163a](https://github.com/ExaDev/github-runner/commit/140163a55801377a5a5cdfcde10fcc584d67bcab))
* make 1Password an optional secrets adapter ([f249f55](https://github.com/ExaDev/github-runner/commit/f249f55422bdd1b68bbff0bebeb8cfc3dbedfb27))
* merge the k3s and k3s-server compose services into one ([f13657a](https://github.com/ExaDev/github-runner/commit/f13657a691aaa3b164384bc4993a75eedcf7fbea))
* orchestrate the github_runner_arc role's bootstrap sequence ([1992d42](https://github.com/ExaDev/github-runner/commit/1992d42af2bfa4fb0a0f27678a75869133da6aa2))
* prove the image pull credential can read each image before writing it ([8ee3b79](https://github.com/ExaDev/github-runner/commit/8ee3b7954cabe000288ee205205ecbfe51e055e0))
* rebuild on Kubernetes (ARC) instead of Docker Compose fleet ([ab10c64](https://github.com/ExaDev/github-runner/commit/ab10c6424c6e705d67d5821ed0b682339db9f10f))
* republish the autoscaler status in the heartbeat gist ([50feb75](https://github.com/ExaDev/github-runner/commit/50feb7536bc3a094d887955baab27ac0bff9ad1e))
* resolve a GitHub App installation ID via a signed JWT ([1244c29](https://github.com/ExaDev/github-runner/commit/1244c2975ce76b35161e7815615ac5524092fe41))
* restrict ARC pods to labelled nodes ([8289cdc](https://github.com/ExaDev/github-runner/commit/8289cdc42c4737e78d8788104cf31950a442f0ac))
* run autoscaler as an in-cluster Deployment, read pressure cluster-wide ([fac57e9](https://github.com/ExaDev/github-runner/commit/fac57e9af204772ccfdf70fe28f12d3e3d5b9cf4))
* run heartbeat as an in-cluster Kubernetes Deployment ([76e82dd](https://github.com/ExaDev/github-runner/commit/76e82dd83f349fe9570e6029e426b65431a3722f))
* run more than one ARC controller replica, spread across nodes ([c042bfa](https://github.com/ExaDev/github-runner/commit/c042bfa7cf64ecb9ff4bdc9271fff863081b31e0))
* run Tailscale inside k3s itself via k3s's native --vpn-auth ([3e63834](https://github.com/ExaDev/github-runner/commit/3e638344e4bb1d64685bc3a895491fd67587623c))
* run the autoscaler as a docker-compose service ([3708d9a](https://github.com/ExaDev/github-runner/commit/3708d9a5923dd4edef85da9904094b62e48c2951))
* template docker-compose's .env from role vars ([bf29037](https://github.com/ExaDev/github-runner/commit/bf290377622356938916354e0314c8b537ea118c))
* trigger an immediate autoscaler reconcile after installing an org ([3728259](https://github.com/ExaDev/github-runner/commit/372825917c773e2caf06a7606b09b6dd9ecc4fab))
* trigger an immediate autoscaler reconcile after installing an org ([e53b764](https://github.com/ExaDev/github-runner/commit/e53b764f0e47aceb3354f1098826fe1740a53e98)), closes [ExaDev/github-runner#3](https://github.com/ExaDev/github-runner/issues/3)
* unpin ordinary CI runner pods from a specific machine, replacing nodeSelector with Guaranteed QoS ([8d0dd71](https://github.com/ExaDev/github-runner/commit/8d0dd71d12aac50e1a3cb9d5474e70c8a96c37de))

### Bug Fixes

* advertise each host's real IP as its k3s node identity ([a2514a1](https://github.com/ExaDev/github-runner/commit/a2514a10aa3332de7943b10b7ca8331faaa9eebc))
* advertise etcd peer traffic on each server's real Tailscale IP ([ff99e1c](https://github.com/ExaDev/github-runner/commit/ff99e1c09483ce6f3f350c92bb0da52b59bfe88a))
* bootstrap the heartbeat gist only when no host in the fleet has one ([b8d9024](https://github.com/ExaDev/github-runner/commit/b8d90242f4d6b3039e1d280e8a9aa2fd588fef98))
* build the runner image on the fleet's own builder profile ([deb3480](https://github.com/ExaDev/github-runner/commit/deb34809d8a785dcfaed3beacf90a250fb1ba1df))
* **cluster:** let the Headscale standby tasks honour DOCKER_HOST ([ddcbf3e](https://github.com/ExaDev/github-runner/commit/ddcbf3e5495c6dbf1c25592bd8bb5e011ddf1bfd))
* create the autoscaler-status ConfigMap only when it is absent ([a5122d5](https://github.com/ExaDev/github-runner/commit/a5122d57802c0fd4cc9aa6ab775d8135d85fe24c))
* declare exadev-runners as a known self-hosted label for actionlint ([3ebb190](https://github.com/ExaDev/github-runner/commit/3ebb19002ace2f3fd46a5f0e54b7e05221483173))
* delete stale kubeconfig on k3s start; rename .env.arc to .env ([c85a6cb](https://github.com/ExaDev/github-runner/commit/c85a6cbd9c010f8c64d39635406f9c640d96cb03))
* derive the play's PATH without depending on ungathered facts ([8b05856](https://github.com/ExaDev/github-runner/commit/8b058566d15ea24b26663ccf7a614925ca37709a))
* detect hadolint's architecture and drop the sudo dependency ([446ea2a](https://github.com/ExaDev/github-runner/commit/446ea2a03b56025b4d62d085d6335682ec20a131))
* drop the global etcd-arg listen-peer-urls override ([e8a6f52](https://github.com/ExaDev/github-runner/commit/e8a6f52561be264052f071852eadf78be8dc453f))
* escape the TLS SAN loop variable and soft-default cross-profile-only env vars ([fc0e325](https://github.com/ExaDev/github-runner/commit/fc0e32537067a0278c5a19ac7944cf17eaeb2d1f))
* export variables from .env.arc so child processes see them ([8c2b7a7](https://github.com/ExaDev/github-runner/commit/8c2b7a71d9d0fada49a1bab8d402023cbec090e3))
* fail fast on a missing repo root, provision a venv, detect the real Docker context, and read every secret in one shell call ([f17973d](https://github.com/ExaDev/github-runner/commit/f17973de4622ecdc8978ead4d0f4dba966bcdcf4))
* fetch the tailnet ACL even in check mode ([3a9d69c](https://github.com/ExaDev/github-runner/commit/3a9d69c7f42e449466a566ff0d20a73ee6308cc1))
* give the heartbeat launchd job a PATH that includes Homebrew ([dc4d1dc](https://github.com/ExaDev/github-runner/commit/dc4d1dc6e7c9856181f0284639c889b09a8392df))
* grant autoscaler core nodes read alongside nodes.metrics.k8s.io ([c672a14](https://github.com/ExaDev/github-runner/commit/c672a142fdf82c91fcc1280c005de2d5740c5732))
* grant autoscaler core pods read alongside pods.metrics.k8s.io ([a54506e](https://github.com/ExaDev/github-runner/commit/a54506ebbe1c83ada7dfda06ae37ba7f38577400))
* heartbeat via host networking; stable k3s node name ([8668334](https://github.com/ExaDev/github-runner/commit/8668334394b14989149e0810692d56e4dea77fed))
* keep mesh-addressed traffic from leaving a node that is off the mesh ([f3d0388](https://github.com/ExaDev/github-runner/commit/f3d0388caa1a73716fd544b3642770f034879aad)), closes [#44](https://github.com/ExaDev/github-runner/issues/44)
* keep the Headscale image local for in-cluster mesh recovery ([a275ebf](https://github.com/ExaDev/github-runner/commit/a275ebfa3cd629ffaf4a3e157baf554b69e4f495))
* keep the Headscale standby's follower out of promotion ([e520ebf](https://github.com/ExaDev/github-runner/commit/e520ebffd8b56055d455b1b83ecc1db362c44a2c))
* keep the temporary Headscale server until recovery has checked it ([d384e5a](https://github.com/ExaDev/github-runner/commit/d384e5a4d701ec432525f0cf5f3b4874ae4f374b))
* key node names and node-password cleanup on the effective node name ([8b10e8e](https://github.com/ExaDev/github-runner/commit/8b10e8e8d32b561175d2ea35b05598b5f5e41fa4))
* let the cluster role's Compose tasks honour DOCKER_HOST ([e7b6eee](https://github.com/ExaDev/github-runner/commit/e7b6eee36ab57e408d42afdcc0034a1fbc612ec2))
* match --disable=traefik --disable-network-policy on joining servers ([60e95d0](https://github.com/ExaDev/github-runner/commit/60e95d0632c05c7c27e2f420835cda36da8203a3))
* name the cluster role in the generated k3s .env header ([4e5f8f7](https://github.com/ExaDev/github-runner/commit/4e5f8f764bb1581b996cade876088458c4ac6fda)), closes [#32](https://github.com/ExaDev/github-runner/issues/32)
* only install Homebrew prerequisites on macOS hosts ([ed29ad2](https://github.com/ExaDev/github-runner/commit/ed29ad274d007b9b09e135caf332ce2fcacc7c69))
* prefer prebuilt wheels when installing Ansible and the kubernetes client ([6a714dc](https://github.com/ExaDev/github-runner/commit/6a714dc11bf23837631d8e2da6144a55c1b5712a))
* read each org's private key once, from the combined secrets read ([42e17b3](https://github.com/ExaDev/github-runner/commit/42e17b316e2ae0413664257ae43f586756a31806))
* remove duplicate homebrew task, guard the node-registration poll ([d36f4a7](https://github.com/ExaDev/github-runner/commit/d36f4a76e5358001b121222e89a66bb87fbff7c9))
* report why an in-cluster mesh recovery did not bring the bootstrap server back ([c1d3ade](https://github.com/ExaDev/github-runner/commit/c1d3ade6c3bbe4eeaa496e3420e8f428589ce2ff))
* require an explicit per-host repo root instead of the control node's ([f4050d7](https://github.com/ExaDev/github-runner/commit/f4050d7b4e89b9ecfa14cb74e398d9dba2f4087b))
* route required-checks through the same runner fallback ([fe3634c](https://github.com/ExaDev/github-runner/commit/fe3634ce147749de594edb9b6159be6e251ef3a2))
* run bootstrap.sh's local play under its own venv's Python ([9b58a81](https://github.com/ExaDev/github-runner/commit/9b58a819f0326dbb72ca6b938350dcb38fb52ca4))
* run the role's read-only lookups under --check ([6ed77c8](https://github.com/ExaDev/github-runner/commit/6ed77c88267be90dc3f982d550b257a097f9142d))
* scope the autoscaler compose service to the primary profile ([5fed3b4](https://github.com/ExaDev/github-runner/commit/5fed3b4672adf44fad0bb2c8252edf6e06fb4344))
* set --node-ip explicitly so config.PrivateIP picks up etcd's real peer address ([019b0e5](https://github.com/ExaDev/github-runner/commit/019b0e51afc2c02ab575a403cf6e4308bde80732))
* skip tailscale up when already authenticated, wait for real DNS before joining ([026b4a1](https://github.com/ExaDev/github-runner/commit/026b4a119124ba20a612c3fed285eb3f185dc979))
* source .env.arc before docker compose up, not after ([eb09869](https://github.com/ExaDev/github-runner/commit/eb0986954d6a176a6bb4f4c38173447cd05e8951))
* stop kubelet merging Tailscale's DNS search domains into pod resolv.conf ([43db6ef](https://github.com/ExaDev/github-runner/commit/43db6efe4b38f60575d9ef7b62a85e339de2ad10))
* tell flannel to use the node's external IP too ([f40dbaf](https://github.com/ExaDev/github-runner/commit/f40dbaf4d8fdfab833f279c1ada296a832f617b7))
* template .env from the combined secrets read, not per-field registers ([f163427](https://github.com/ExaDev/github-runner/commit/f163427a85b0e19b6d35f9bc848a60a8777fa839))
* use --node-external-ip, not --node-ip, for cross-node identity ([e291eec](https://github.com/ExaDev/github-runner/commit/e291eec25e7738eef5f82baff634d094556e7216))
* use an explicit if-block for the meminfo-read guard in autoscaler.sh ([26b0afe](https://github.com/ExaDev/github-runner/commit/26b0afe170cc0fec96ed7cb8925b9f43745195f3))
* wait for a node to actually exist, not just API reachability ([1e1bdc9](https://github.com/ExaDev/github-runner/commit/1e1bdc98525fa5afc915a920caaa2f5b518b44e3))
* wait for the bootstrap server to be a mesh peer before joining ([9c832fd](https://github.com/ExaDev/github-runner/commit/9c832fd8797a2ee3d0dc08a6628d2b2248bdddb2))
* word the generated .env header for both deployment paths ([b13ee04](https://github.com/ExaDev/github-runner/commit/b13ee043a1dcf266eb71c9af7b23610edc2887af))
* wrap the bun install pipe in bash -c for pipefail ([094ad1f](https://github.com/ExaDev/github-runner/commit/094ad1ffda5bfbd443ad558f88a5fa0d5ffa4f02))

### Code Refactoring

* **cluster:** ship the node runtime in the collection and run it from github_runner_cluster_dir ([a30fc6c](https://github.com/ExaDev/github-runner/commit/a30fc6cd310753dcb612f828aa0d67f51f419fd4))
* move ExaDev's fleet inventory and Helm values to github-runner-fleet ([e08b3af](https://github.com/ExaDev/github-runner/commit/e08b3afaf73dd3c175cf368b3ebf85070dfcbfac))
* move roles and playbook to an Ansible collection layout ([e2963cc](https://github.com/ExaDev/github-runner/commit/e2963cc5a8699c760de8241b1a63e6835a799bcc))
* render ARC values on the control node and validate before the cluster role ([28d8144](https://github.com/ExaDev/github-runner/commit/28d8144960ae2ad655fb61e95f52b1b38718377b))
* route the tailscale mesh through a provider interface ([8ec62ed](https://github.com/ExaDev/github-runner/commit/8ec62ed428f03f8a7f2b82262c86eb279e69a570))
* run bootstrap.sh through the Ansible role instead of duplicating it ([802d41c](https://github.com/ExaDev/github-runner/commit/802d41cab852a7217676b444d04b0e932dfd9c0f))
* sign the GitHub App JWT with a tested script instead of inline shell ([e356764](https://github.com/ExaDev/github-runner/commit/e3567643326a69a77413e2535f955d73d8c866f6))
* split cluster, ARC, secrets and k8s client roles ([88ed5cc](https://github.com/ExaDev/github-runner/commit/88ed5cc702b4990d84b1f22bcbd4d8f68e794f60))

### Documentation

* add a changelog file for the collection ([3d1b428](https://github.com/ExaDev/github-runner/commit/3d1b428a5d3bba4d4ce73c516ca1e98eb6ebdda0))
* add ansible-vault, env and 1Password example inventories ([184e969](https://github.com/ExaDev/github-runner/commit/184e969cdcc5c6e43ff35844c89c3db48dd61727))
* anchor determine-runner on exadev-runners as a deliberate choice ([1a8ab81](https://github.com/ExaDev/github-runner/commit/1a8ab81e1491b1b28f0654ca51694d533360ed59))
* correct the runner image build instructions ([562e93c](https://github.com/ExaDev/github-runner/commit/562e93cab9c5c7bb9666416cec7cb791f3ab3782)), closes [#10](https://github.com/ExaDev/github-runner/issues/10)
* describe bootstrap.sh as a local wrapper around the Ansible role ([5b06538](https://github.com/ExaDev/github-runner/commit/5b065389051a980250299b2899fd1421ae732514))
* describe the ARC role's profiles, validation, sizing and App setup ([ffc708a](https://github.com/ExaDev/github-runner/commit/ffc708ab44775836d2da785833a478a4e6424a0d))
* describe the collection without ExaDev's own hosts ([e929d19](https://github.com/ExaDev/github-runner/commit/e929d1989efd8255cb4096db554bfcbfcc4d5143))
* describe the two-role model and in-cluster fleet-health platform ([e1bd85b](https://github.com/ExaDev/github-runner/commit/e1bd85b2fd95dff78d7faa17db1df224d828c72e))
* document Headscale backups, the warm standby and their RTO and RPO ([e713ead](https://github.com/ExaDev/github-runner/commit/e713ead01e5fb2e1285b2a9339b434dfa849159e))
* document node roles, scale-set profiles, and Ansible's verified status ([bc1216b](https://github.com/ExaDev/github-runner/commit/bc1216b0e4a8545ead251ed80ff5c7b0d45644f3))
* document the Ansible deployment path ([25a9e1d](https://github.com/ExaDev/github-runner/commit/25a9e1df2181b92fbc1a7c94b7bb426b672b043a))
* document the mesh providers and add example inventories ([ca4d2f0](https://github.com/ExaDev/github-runner/commit/ca4d2f03b0a83e04422c6e3c07fc3f660a1d60bc))
* document the release process and drop the stale ubuntu-latest claim ([4078490](https://github.com/ExaDev/github-runner/commit/4078490daab01619d5bd462a5f72ffeb3690f96b)), closes [#10](https://github.com/ExaDev/github-runner/issues/10)
* document the stale AutoscalingListener EphemeralRunnerSet-not-found trap ([85ed7ac](https://github.com/ExaDev/github-runner/commit/85ed7ac0acd9c4e4e004d4ac8a0fcf34d05715e0))
* document the Tailscale vpn-auth setup and its DNS search-domain pitfall ([94d2449](https://github.com/ExaDev/github-runner/commit/94d2449f1c22d319dd59131396238a13646849f2))
* document the three node roles and the etcd-over-Tailscale HA gotchas ([e02e222](https://github.com/ExaDev/github-runner/commit/e02e2222f96b378605efdcd3fec0083f959b7aa3))
* document the usage-driven autoscaler ([b6dbf61](https://github.com/ExaDev/github-runner/commit/b6dbf61d6322cd0cfcbd2e1aad0ed4cad769426c))
* drop references to issues from before the repository was published ([8e830a1](https://github.com/ExaDev/github-runner/commit/8e830a107d2f423463441ac26292e2b2784a6b38))
* point stale build-runner-image.yml references at release.yml ([46432fe](https://github.com/ExaDev/github-runner/commit/46432fe79d8987957145bffa196836e838a9a84d))
* restructure README into standard getting-started/architecture/conventions sections ([4019942](https://github.com/ExaDev/github-runner/commit/4019942fe273331221b0d8837ea2d7f8756df1d8))
* say the k3s runtime files are mounted from the role's project directory ([13b2368](https://github.com/ExaDev/github-runner/commit/13b2368e1e90837523ea012a6d272d77882f3229))
* symlink AGENTS.md and CLAUDE.md to README.md ([d5fe140](https://github.com/ExaDev/github-runner/commit/d5fe1403280d6ceabdbb56a6adbe1b2e78d7da5f))

### Styles

* add the second space before the requirements comment ([42203b8](https://github.com/ExaDev/github-runner/commit/42203b8df61f55a5a7b39edbafc4ad6cb15ab060))
* put two spaces before the release workflow's permission comments ([abf1182](https://github.com/ExaDev/github-runner/commit/abf1182415eccc91ee5f04c9025e5b51c9e954a8))

### Tests

* add a Headscale mesh integration test ([9551c78](https://github.com/ExaDev/github-runner/commit/9551c7873913e19b239fcb6e7443260bae7d4ca8))
* add a permanent smoke-test workflow for the ARC scale set ([88f9f05](https://github.com/ExaDev/github-runner/commit/88f9f05057258dfe916545c3ad9ce4c9a34f95af))
* drop the egress and network-state probes from the mesh test ([8f17f0d](https://github.com/ExaDev/github-runner/commit/8f17f0daee849ae0782f095a055a0cfdad71dfb3))
* exercise Litestream replication and Headscale standby promotion ([0e2449b](https://github.com/ExaDev/github-runner/commit/0e2449b1fdbf433d8b0ff0bced66b444ecbcb240))
* give the Litestream test a port of its own ([d10dd48](https://github.com/ExaDev/github-runner/commit/d10dd4818609c2661235c068a8abde446395c202))
* probe which runner picks up runs-on: ubuntu-latest ([2a6d8c9](https://github.com/ExaDev/github-runner/commit/2a6d8c915bcda85de067b5314dad9da2fc0fc46b))
* record the runner's egress throughout the mesh test ([878c444](https://github.com/ExaDev/github-runner/commit/878c444ba06da58337b9bc2caca3e78980d8ed74))
* run the mesh test against the role-managed cluster directory ([df42184](https://github.com/ExaDev/github-runner/commit/df421846696822205b5cf59856e0b0cada72c692))
* snapshot the Docker host's network before the cold restart and on failure ([a24809b](https://github.com/ExaDev/github-runner/commit/a24809bd93480340e49dcf6ecd3d1c1a46747ca8))
* wait for the mesh to converge before failing a pod reachability check ([41b9205](https://github.com/ExaDev/github-runner/commit/41b9205485ef69c756140a55a898a2a0512605be))

### Build System

* add semantic-release and commitlint devDependencies ([09d5539](https://github.com/ExaDev/github-runner/commit/09d55392c22e236d6fedf417681969aa1380e0e5))
* **release:** bump docker/metadata-action to its current major ([9770bc9](https://github.com/ExaDev/github-runner/commit/9770bc9626ed298a6a44873618d980d410fed66b))

### Continuous Integration

* always install the pinned Docker Compose for the mesh and Litestream tests ([ee9ddcd](https://github.com/ExaDev/github-runner/commit/ee9ddcdde604428aa6a4ba16181363fd565e5d9b))
* drop the input that ran the mesh test on ubuntu-latest ([7c56ba2](https://github.com/ExaDev/github-runner/commit/7c56ba284ef7ba325f93861e8446de73034e9635))
* let a manual mesh integration run choose ubuntu-latest ([282de72](https://github.com/ExaDev/github-runner/commit/282de72fc4bb7366084987f50e6cdfaa0f0279d4))
* lint the collection with ansible-lint's production profile ([1698dd2](https://github.com/ExaDev/github-runner/commit/1698dd2e6fb070d2eaf94535d41138f0eacb63b2))
* push release commits with a deploy key instead of a GitHub App token ([4dcd7b2](https://github.com/ExaDev/github-runner/commit/4dcd7b28b306a3832b377b2509914527c7535e3a))
* run CI, mesh and Litestream tests on GitHub-hosted runners ([47f9077](https://github.com/ExaDev/github-runner/commit/47f9077d7d4e0099448fb1211672e66f762777ae))
* run commitlint from the repo and put ansible-lint on PATH ([e49cc21](https://github.com/ExaDev/github-runner/commit/e49cc21e132aa96a5d6ddc7968737dfa302122cc))
* run the release and image builds on GitHub-hosted runners ([cccdfc6](https://github.com/ExaDev/github-runner/commit/cccdfc666a481474a46c97d34ab901357d17c482))
* syntax-check every top-level playbook ([dc26796](https://github.com/ExaDev/github-runner/commit/dc26796ffbdc2d4bcb41fdc613fada488ab7993c)), closes [#30](https://github.com/ExaDev/github-runner/issues/30)

### Miscellaneous Chores

* add a yamllint config for this project's own conventions ([d635bf1](https://github.com/ExaDev/github-runner/commit/d635bf18820a392fa342793d06447897dcd2140f))
* declare the collection's Ansible requirement and the secrets role's cross-role facts ([633fd4c](https://github.com/ExaDev/github-runner/commit/633fd4ccbf84cf19963fb50d456a702be8295d29))
* exclude release tooling from the built collection and git ([ce9e4b5](https://github.com/ExaDev/github-runner/commit/ce9e4b5eb5f3a965552daa82aa3a9ba8274a6ef5))
* license the project under MIT ([29913c3](https://github.com/ExaDev/github-runner/commit/29913c33de70f5c23d9e7973575f5b60c8636af7))
* unignore CLAUDE.md and AGENTS.md ([981f79b](https://github.com/ExaDev/github-runner/commit/981f79bb968426a52cde472811aca9524a7b2b46))

# Changelog

Release entries are generated automatically at release time and prepended above this line.
