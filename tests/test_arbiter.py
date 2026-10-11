import threading

from rafiq.arbiter import Arbiter


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_the_microphone_with_the_closest_match_answers():
    clock = Clock()
    a = Arbiter(window=0.18, lock=4.0, clock=clock)
    a.offer("pc", 0.80)
    clock.t += 0.05
    a.offer("kitchen", 0.40)
    assert a.winner() is None                 # still listening for others
    clock.t += 0.2
    assert a.winner() == "kitchen"


def test_a_single_microphone_wins_once_the_window_closes():
    clock = Clock()
    a = Arbiter(clock=clock)
    a.offer("pc", 0.9)
    clock.t += 0.2
    assert a.winner() == "pc"


def test_nothing_offered_means_no_winner():
    assert Arbiter(clock=Clock()).winner() is None


def test_a_late_offer_after_the_window_does_not_answer_during_the_lock():
    clock = Clock()
    a = Arbiter(window=0.18, lock=4.0, clock=clock)
    a.offer("pc", 0.9)
    clock.t += 0.3
    assert a.winner() == "pc"
    assert a.offer("kitchen", 0.1) is False   # an echo of the same "Rafiq"
    clock.t += 1.0
    assert a.winner() == "pc"


def test_after_the_lock_a_new_round_starts():
    clock = Clock()
    a = Arbiter(window=0.18, lock=4.0, clock=clock)
    a.offer("pc", 0.9)
    clock.t += 0.3
    assert a.winner() == "pc"
    clock.t += 4.0
    assert a.offer("kitchen", 0.5) is True
    clock.t += 0.2
    assert a.winner() == "kitchen"


def test_scores_are_compared_against_each_microphones_own_threshold():
    clock = Clock()
    a = Arbiter(clock=clock)
    a.offer("pc", 0.6, threshold=1.0)         # 60% of its threshold
    a.offer("kitchen", 0.5, threshold=0.5)    # right at its threshold: barely
    clock.t += 0.2
    assert a.winner() == "pc"


def test_priority_breaks_a_tie():
    clock = Clock()
    a = Arbiter(clock=clock, priority={"kitchen": 1})
    a.offer("pc", 0.5)
    a.offer("kitchen", 0.5)
    clock.t += 0.2
    assert a.winner() == "kitchen"


def test_a_microphone_offering_twice_keeps_its_best_score():
    clock = Clock()
    a = Arbiter(clock=clock)
    a.offer("pc", 0.3)
    a.offer("kitchen", 0.4)
    a.offer("pc", 0.9)
    clock.t += 0.2
    assert a.winner() == "pc"


def test_claim_waits_out_the_window_and_tells_each_microphone_if_it_won():
    a = Arbiter(window=0.05, lock=4.0)
    results = {}

    def hear(mic, score):
        results[mic] = a.claim(mic, score)

    threads = [threading.Thread(target=hear, args=(m, s)) for m, s in (("pc", 0.7), ("kitchen", 0.3))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2)
    assert results == {"pc": False, "kitchen": True}
