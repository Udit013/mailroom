from mailroom.supervisor import RestartPolicy


def test_delays_grow_then_the_policy_gives_up_within_the_window():
    policy = RestartPolicy(max_restarts=5, window_seconds=300)

    delays = [policy.next_delay(now=float(second)) for second in range(6)]

    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, None]


def test_restarts_older_than_the_window_are_forgotten():
    policy = RestartPolicy(max_restarts=2, window_seconds=10)
    policy.next_delay(now=0)
    policy.next_delay(now=1)

    assert policy.next_delay(now=5) is None
    assert policy.next_delay(now=20) == 1.0


def test_delay_is_capped():
    policy = RestartPolicy(max_restarts=20, window_seconds=1000, max_delay_seconds=30)

    delays = [policy.next_delay(now=float(second)) for second in range(8)]

    assert max(delay for delay in delays if delay is not None) == 30
