# The regrade service of the cluster role's Compose file (see docker-compose.yml): bash, jq and the Docker CLI, all from Alpine's apk. The scripts it runs are not copied in: Compose mounts this directory, so a host picks up a collection upgrade when the service restarts, without an image Compose would not otherwise rebuild.
FROM alpine:3.22

RUN apk add --no-cache bash jq docker-cli \
 && ln -s /usr/local/lib/fleet-regrade/fleet-regrade.sh /usr/local/bin/fleet-regrade

ENTRYPOINT ["/usr/local/bin/fleet-regrade"]
CMD ["watch"]
