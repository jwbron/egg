# BRC Consensus History — apply phase

Generated: 2026-06-09T18:25:12Z
Pipeline: issue-3023

### [2026-06-09T18:25:06Z] reviewer_code (HEARTBEAT): heartbeat: WORKING

event-pump woke (rc=0) (slice=slice-3)

````yaml
id: 7f45d79b-568a-4a
phase: apply
metadata:
  state: WORKING
  slice_id: slice-3
````

### [2026-06-09T18:25:06Z] reviewer_code (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

event-pump wait role=reviewer_code (slice=slice-3)

````yaml
id: 9ba0cd98-d153-41
phase: apply
metadata:
  state: WAITING_FOR_EVENT
  slice_id: slice-3
````

### [2026-06-09T18:25:11Z] applier (HEARTBEAT): heartbeat: WORKING

event-pump start (slice=none)

````yaml
id: 596e709c-304b-4d
phase: apply
metadata:
  state: WORKING
````

### [2026-06-09T18:25:11Z] reviewer_contract (HEARTBEAT): heartbeat: WORKING

event-pump start (slice=none)

````yaml
id: 53b0fa19-6e72-49
phase: apply
metadata:
  state: WORKING
````

### [2026-06-09T18:25:12Z] applier (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

event-pump wait role=applier (slice=none)

````yaml
id: 409c0928-fc0b-49
phase: apply
metadata:
  state: WAITING_FOR_EVENT
````

### [2026-06-09T18:25:12Z] reviewer_contract (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

event-pump wait role=reviewer_contract (slice=none)

````yaml
id: 40003531-a20a-4c
phase: apply
metadata:
  state: WAITING_FOR_EVENT
````
