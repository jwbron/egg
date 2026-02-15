# bin/

Convenient symlinks to commonly used commands.

## Commands

- `egg` - Start/manage egg sandbox container
- `egg-sdlc` - Interactive SDLC pipeline CLI with DAG visualization and HITL checkpoints
- `egg-onboarding-docs` - Generate onboarding documentation for a repository
- `egg-deploy` - Deploy and manage the gateway stack via Docker Compose
- `egg-status` - Monitor all active SDLC pipelines in real-time
- `egg-pipeline-watch` - Watch a specific pipeline's progress with DAG visualization
- `setup-gateway` - Install gateway sidecar service

## Note

Most commands are symlinks to the actual files in `gateway/` and `sandbox/`.
The real files live with their respective services for better organization.
