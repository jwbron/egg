FROM alpine:latest
RUN apk add --no-cache curl
COPY phase-runner.sh /phase-runner.sh
RUN chmod +x /phase-runner.sh
ENTRYPOINT ["/phase-runner.sh"]
