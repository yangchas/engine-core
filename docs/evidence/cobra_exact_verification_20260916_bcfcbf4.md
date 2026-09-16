# Exact cobra-ion verification: final evidence HEAD `bcfcbf4`

The final evidence/documentation HEAD was archived and extracted into the new
remote directory `/home/exedev/validation/engine-core-bcfcbf4`.

```text
archive SHA-256: 6cbab641776e76c54324fa0bd897f124057c5370b1f30ca86802201af73cf9d8
Python:          3.12.3
pytest:          421 passed in 2.06s
compileall:      PASS
source build id: f8c88da7d633d27b57fd2249496d17f4d734c3cc6b31dfcbf749c5ed6cc59db05
```

The local archive hash is identical and the local full suite remains
`421 passed`. The only changes after the code verification commit are
evidence/documentation files; this run verifies the exact final repository
HEAD as well.

`engine-next.service` and `t1-v2-live.service` remained active with
`NRestarts=0` (`MainPID=379553` and `2878024` respectively). No Rabbit
consumer/ACK change, Redis/TD write, restart, notification, or effect was
performed.

