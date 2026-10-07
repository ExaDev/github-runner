# [1.20.0](https://github.com/ExaDev/github-runner/compare/v1.19.0...v1.20.0) (2026-10-07)


### Bug Fixes

* **autoscaler:** count runners from ephemeral runners and patch the state ConfigMap from a file ([0da00e9](https://github.com/ExaDev/github-runner/commit/0da00e919a6a8e9f15330f87d2b85bdae5b9b9dd))


### Features

* **autoscaler:** move a slot to a saturated pooled profile when the pool is at its ceiling ([0e75801](https://github.com/ExaDev/github-runner/commit/0e75801c4f3ec7f926ea551a254278f00cf8dcaa))

# [1.19.0](https://github.com/ExaDev/github-runner/compare/v1.18.0...v1.19.0) (2026-10-07)


### Features

* **arc:** add system-library capabilities ([fab2a78](https://github.com/ExaDev/github-runner/commit/fab2a78141a32d030b94d43b28e7470aaf8d1db3))
* **tool-images:** add reference system-library payload images ([4b2b973](https://github.com/ExaDev/github-runner/commit/4b2b9730a862290249dcc508edf7d4e9f3f48ba5))

# [1.18.0](https://github.com/ExaDev/github-runner/compare/v1.17.2...v1.18.0) (2026-10-07)


### Features

* **arc:** add capabilities to scale-set profiles ([e0e6ce9](https://github.com/ExaDev/github-runner/commit/e0e6ce9817d9ed39cb0174879ffddb080d2411ca)), closes [#55](https://github.com/ExaDev/github-runner/issues/55)
* **tool-images:** add reference tool-cache capability images ([f33107f](https://github.com/ExaDev/github-runner/commit/f33107fa7693b3040bff3d9b30db06785e46ab1f)), closes [#55](https://github.com/ExaDev/github-runner/issues/55)

## [1.17.2](https://github.com/ExaDev/github-runner/compare/v1.17.1...v1.17.2) (2026-10-07)

## [1.17.1](https://github.com/ExaDev/github-runner/compare/v1.17.0...v1.17.1) (2026-10-04)


### Bug Fixes

* **release:** commit the cluster role's stamped version with the release ([1f335cd](https://github.com/ExaDev/github-runner/commit/1f335cd65ab9cb90a71e085a1264a67f8eba4614))

# [1.17.0](https://github.com/ExaDev/github-runner/compare/v1.16.0...v1.17.0) (2026-10-04)


### Bug Fixes

* **arc:** restore the collection version and deployed-state defaults the release stamps ([beeba32](https://github.com/ExaDev/github-runner/commit/beeba32e965f77d1adfc6dea80d6142f6f2b664f))


### Features

* **cluster:** let a host without API access skip the node version stamp ([5d3829b](https://github.com/ExaDev/github-runner/commit/5d3829b6f5ebccf842aef3e20fb01dfc54064541))
* publish each scope's deployed collection version in fleet-health.json ([485ea54](https://github.com/ExaDev/github-runner/commit/485ea54901dfd2fdb0e4eccea800858cf8bbdcf1))

# [1.16.0](https://github.com/ExaDev/github-runner/compare/v1.15.0...v1.16.0) (2026-10-04)


### Features

* **heartbeat:** show how broad and how long the measured peaks are ([a9d8076](https://github.com/ExaDev/github-runner/commit/a9d8076ae8a253d44a58780a73797360ee0f6ad8))

# [1.15.0](https://github.com/ExaDev/github-runner/compare/v1.14.0...v1.15.0) (2026-10-03)


### Features

* **heartbeat:** publish the distribution of measured job peak memory ([9434c80](https://github.com/ExaDev/github-runner/commit/9434c8088a79842c0ae409fad21ae63501ac30be))

# [1.14.0](https://github.com/ExaDev/github-runner/compare/v1.13.0...v1.14.0) (2026-10-03)


### Features

* **arc:** let runner pods create events so jobs can record their peak memory ([8fcfe9d](https://github.com/ExaDev/github-runner/commit/8fcfe9d81987ac240685dfd3dde104f809b8754a))
* **autoscaler:** collect the peaks the runners measured ([b72bf02](https://github.com/ExaDev/github-runner/commit/b72bf0263117b5acacc4671eb7cf920d142aebb5))
* **runner:** record each job's peak memory from a job-completed hook ([850bbd8](https://github.com/ExaDev/github-runner/commit/850bbd8acb0ba2cfdc07776b90e872ad3c829256))

# [1.13.0](https://github.com/ExaDev/github-runner/compare/v1.12.0...v1.13.0) (2026-10-03)


### Features

* **autoscaler:** record each job's observed peak memory ([77ac920](https://github.com/ExaDev/github-runner/commit/77ac920e544d8b9872847afd87d1cfa7b180abe8))

# [1.12.0](https://github.com/ExaDev/github-runner/compare/v1.11.1...v1.12.0) (2026-10-03)


### Features

* **arc:** refuse a controller version change while runners are running ([eeb7259](https://github.com/ExaDev/github-runner/commit/eeb7259e45b218581fab70faf69f9cd8c2f19961))

## [1.11.1](https://github.com/ExaDev/github-runner/compare/v1.11.0...v1.11.1) (2026-10-03)

# [1.11.0](https://github.com/ExaDev/github-runner/compare/v1.10.4...v1.11.0) (2026-10-03)


### Features

* **arc:** apply the controller chart's CRDs before installing or upgrading it ([78ae73c](https://github.com/ExaDev/github-runner/commit/78ae73c57d9ca91276d4f8f16f6c5a17ca40ecce))

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

# [1.10.0](https://github.com/ExaDev/github-runner/compare/v1.9.0...v1.10.0) (2026-10-03)


### Bug Fixes

* **cluster:** leave the docker-prune container unnamed so projects on one daemon do not collide ([1a7133e](https://github.com/ExaDev/github-runner/commit/1a7133ef3c69a59a208559acf643acdddec1643e))


### Features

* **cluster:** prune stale build cache and dangling images on every host ([e683de7](https://github.com/ExaDev/github-runner/commit/e683de7d83b2eeeb2942b8e922885912de6a05a4))

# [1.9.0](https://github.com/ExaDev/github-runner/compare/v1.8.1...v1.9.0) (2026-10-03)


### Bug Fixes

* **platform:** render the priority class as a template the node recovery test also applies ([bcbdab7](https://github.com/ExaDev/github-runner/commit/bcbdab7f780b33e1157cdd4be694238c41de36a8))


### Features

* **heartbeat:** publish why the fleet is unhealthy and which nodes are under pressure ([56cb613](https://github.com/ExaDev/github-runner/commit/56cb6138d5d36900355f659d61490675cb3d625f))
* **platform:** run the platform pods above runner pods with a local-storage request ([d6d4a69](https://github.com/ExaDev/github-runner/commit/d6d4a69a3f20e57e1ac03f3b8d2eea00abbb3af1))

## [1.8.1](https://github.com/ExaDev/github-runner/compare/v1.8.0...v1.8.1) (2026-10-03)

# [1.8.0](https://github.com/ExaDev/github-runner/compare/v1.7.0...v1.8.0) (2026-10-03)


### Bug Fixes

* **autoscaler:** read every target before failing safe, lower on unschedulable runner pods ([9c3a824](https://github.com/ExaDev/github-runner/commit/9c3a8243a576f2763e163619a6026ab2f64a7ae9))


### Features

* **heartbeat:** stop vouching for the fleet while runner pods cannot be scheduled ([f875714](https://github.com/ExaDev/github-runner/commit/f875714c016bac0a01fced9b1983b2b818457903))

# [1.7.0](https://github.com/ExaDev/github-runner/compare/v1.6.0...v1.7.0) (2026-09-30)


### Features

* **arc:** let a profile run on burst nodes only ([44de91b](https://github.com/ExaDev/github-runner/commit/44de91b6d5cec772cc4efe21be45d25417370cd2))

# [1.6.0](https://github.com/ExaDev/github-runner/compare/v1.5.2...v1.6.0) (2026-09-30)


### Features

* **arc:** let a sized profile's runner pods overflow onto burst nodes ([94a0b2b](https://github.com/ExaDev/github-runner/commit/94a0b2bfc08c1604f6b9c324f3ef6480b794f7a9))

## [1.5.2](https://github.com/ExaDev/github-runner/compare/v1.5.1...v1.5.2) (2026-09-28)


### Bug Fixes

* register gcc-11/g++-11 as the generic gcc/g++/cc/c++ alternatives ([ebfd896](https://github.com/ExaDev/github-runner/commit/ebfd8964633356943fdbb18604f2b2875b8a570a))

## [1.5.1](https://github.com/ExaDev/github-runner/compare/v1.5.0...v1.5.1) (2026-09-28)


### Bug Fixes

* install make in the runner image for native addon builds ([e53aad6](https://github.com/ExaDev/github-runner/commit/e53aad62e588446332b050b55b1e8783033ded7f))

# [1.5.0](https://github.com/ExaDev/github-runner/compare/v1.4.0...v1.5.0) (2026-09-28)


### Bug Fixes

* **arc:** drop unnecessary $/${} on arithmetic array subscripts ([78be6df](https://github.com/ExaDev/github-runner/commit/78be6dfdcecf211cda61c0b64d99d3207b646423))


### Features

* **arc:** pool multiple autoscaled profiles into one shared budget ([82eddac](https://github.com/ExaDev/github-runner/commit/82eddacd5891c01bf15cb88ae1e6fa10ae11e624))

# [1.4.0](https://github.com/ExaDev/github-runner/compare/v1.3.1...v1.4.0) (2026-09-26)


### Features

* **cluster:** pin the Docker context the role drives per host ([63bb283](https://github.com/ExaDev/github-runner/commit/63bb283677690b213d1b17c1a0c046265ee2272c))

## [1.3.1](https://github.com/ExaDev/github-runner/compare/v1.3.0...v1.3.1) (2026-09-26)


### Bug Fixes

* **cluster:** refuse Docker Compose 2.37.1 to 2.38.x ([b99ae54](https://github.com/ExaDev/github-runner/commit/b99ae54e1576eab76b6d67cd202f126c83d81149))

# [1.3.0](https://github.com/ExaDev/github-runner/compare/v1.2.2...v1.3.0) (2026-09-26)


### Features

* **arc:** taint a stopped node out of service so its pods are replaced ([8d6d44b](https://github.com/ExaDev/github-runner/commit/8d6d44b8bb4b78f388f02aed05a8c52772f52abb)), closes [#2](https://github.com/ExaDev/github-runner/issues/2)

## [1.2.2](https://github.com/ExaDev/github-runner/compare/v1.2.1...v1.2.2) (2026-09-26)


### Bug Fixes

* **arc:** read the App's id from an existing App Secret ([e0854d2](https://github.com/ExaDev/github-runner/commit/e0854d24506d26650e3197d6d7f425ca1814f36e))

## [1.2.1](https://github.com/ExaDev/github-runner/compare/v1.2.0...v1.2.1) (2026-09-26)


### Bug Fixes

* **arc:** count this host's orgs however they were supplied ([c731751](https://github.com/ExaDev/github-runner/commit/c7317512d4c47098b1342fd507df9d072470d156))

# [1.2.0](https://github.com/ExaDev/github-runner/compare/v1.1.0...v1.2.0) (2026-09-26)


### Bug Fixes

* **arc:** keep each org's resolved installation id to that org ([6c1b65a](https://github.com/ExaDev/github-runner/commit/6c1b65a1b0026241d57b1a186c0bee5b80f1973b))


### Features

* **arc:** let the App setup write the App Secret and remove the key ([7f64f21](https://github.com/ExaDev/github-runner/commit/7f64f21bea9d9f2a3689b1be26d7a27122fa6054))
* **arc:** measure the runner ceiling from each node's free capacity ([a9a498b](https://github.com/ExaDev/github-runner/commit/a9a498b7c03b5cd1741b0b71cf53cd93cf96f1dc))
* **arc:** mint the image pull secret from the runners' GitHub App ([6d1c8a3](https://github.com/ExaDev/github-runner/commit/6d1c8a3c17555cf5b9d57608d896c59af74335f6))

# [1.1.0](https://github.com/ExaDev/github-runner/compare/v1.0.3...v1.1.0) (2026-09-25)


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

# 1.0.0 (2026-09-25)


* feat!: make 1Password an optional secrets adapter ([f249f55](https://github.com/ExaDev/github-runner/commit/f249f55422bdd1b68bbff0bebeb8cfc3dbedfb27))
* feat(arc)!: read each org's App key from its private_key ([ac1b879](https://github.com/ExaDev/github-runner/commit/ac1b879069fdb2f8ee60862879350476d13adcec))
* feat(cluster)!: keep new join keys without calling the secrets role ([3befb78](https://github.com/ExaDev/github-runner/commit/3befb78643164d79c09467c115c127a498035c04))
* refactor!: move ExaDev's fleet inventory and Helm values to github-runner-fleet ([e08b3af](https://github.com/ExaDev/github-runner/commit/e08b3afaf73dd3c175cf368b3ebf85070dfcbfac))
* refactor!: render ARC values on the control node and validate before the cluster role ([28d8144](https://github.com/ExaDev/github-runner/commit/28d8144960ae2ad655fb61e95f52b1b38718377b))
* refactor(cluster)!: ship the node runtime in the collection and run it from github_runner_cluster_dir ([a30fc6c](https://github.com/ExaDev/github-runner/commit/a30fc6cd310753dcb612f828aa0d67f51f419fd4))


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
* build and push all three images from the release workflow ([de104de](https://github.com/ExaDev/github-runner/commit/de104de92b20e28fff4e086ec0c75b8ba12ec9b1)), closes [#10](https://github.com/ExaDev/github-runner/issues/10)
* check ARC secrets exist before changing anything, and support pre-created ones ([63d42e7](https://github.com/ExaDev/github-runner/commit/63d42e737a7bbe15915ca5aae6cacc929255a8db))
* **ci:** add commitlint, ansible-lint, and a collection build check ([dda2978](https://github.com/ExaDev/github-runner/commit/dda29786c4c5e3d5e008c80723c03a942f2e10c8))
* **cluster:** back up Headscale with Litestream and keep a warm standby ([e538f98](https://github.com/ExaDev/github-runner/commit/e538f987bdf7f155cfff3d041692ba184ce68b5f))
* configure semantic-release for the collection version ([d190ed6](https://github.com/ExaDev/github-runner/commit/d190ed67b2ed7af1c8b78011005d7dee33bf4d19))
* create the runners' GitHub App through the manifest flow ([9b8412f](https://github.com/ExaDev/github-runner/commit/9b8412f3cea1d66844619706896b4ddbea4aca1d))
* define github_runner_arc role defaults ([3736baf](https://github.com/ExaDev/github-runner/commit/3736bafee071326c9fd523ea8022d143415e6645))
* generalize the primary role into server/agent plus a fleet-health installer ([7aec978](https://github.com/ExaDev/github-runner/commit/7aec97802c140fb1db42c52573882dd55c13adc7))
* genuine 3-node k3s embedded-etcd HA control plane ([72e4b76](https://github.com/ExaDev/github-runner/commit/72e4b7622f4cae9be6d54752b99507260f1a014f))
* heartbeat as a docker-compose service refreshing a secret gist ([9f78ea7](https://github.com/ExaDev/github-runner/commit/9f78ea7244a1571b44808cc7c04920888f415139))
* initial ExaDev self-hosted runner setup ([8665b12](https://github.com/ExaDev/github-runner/commit/8665b12c283478c61d56fec2631d4ad3b80abb4f))
* install one org's namespace, secrets, and runner scale set ([42fdcf8](https://github.com/ExaDev/github-runner/commit/42fdcf897af6ab5cc039616340f81bcd86962fcc))
* let nodes register with a control server other than Tailscale's ([140163a](https://github.com/ExaDev/github-runner/commit/140163a55801377a5a5cdfcde10fcc584d67bcab))
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


### BREAKING CHANGES

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
* github_runner_cluster_repo_root is replaced by github_runner_cluster_dir, with no alias. A host that ran Compose in a checkout keeps its cluster by setting github_runner_cluster_dir to that checkout, and, if the checkout's directory is not called github-runner, github_runner_cluster_compose_project to that directory's name.
* the github_runner_secrets role is renamed to
github_runner_secrets_onepassword and runs only with
github_runner_secrets_source: onepassword. Its variables are renamed
(github_runner_secrets_vault, _item and _cache_path become
github_runner_secrets_onepassword_vault, _item and _cache_path), and
github_runner_secrets_provided is gone: set the cluster and ARC roles'
variables directly.
* github_runner_cluster_create_tailscale_join_key and
github_runner_cluster_create_headscale_join_key are replaced by
github_runner_cluster_join_key_path and
github_runner_cluster_join_key_listener.
* github_runner_arc_org_private_keys is gone. Put each
org's PEM key in its private_key field instead, and drop
org_private_keys from github_runner_secrets_provided.
* github_runner_arc_values_dir is a control-node path with
no default; the autoscaler is installed only for a profile that sets
autoscale: true; github_runner_arc_autoscaler_usable_budget_gi,
_max_ceiling and _floor have no defaults; and the autoscaler image needs
AUTOSCALER_NAMESPACE, AUTOSCALER_RELEASE_NAME and the pool figures set.

# Changelog

Release entries are generated automatically at release time and prepended above this line.
