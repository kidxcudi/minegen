"""Random-dataset baseline: same schema, same budget, no LLM. The fair comparison for the hypothesis."""
import random

from .validator import validate_all


def random_proposals(n, seed=0):
    rng = random.Random(seed)
    out = []
    for i in range(n * 3):  # oversample; the validator drops duplicates
        vocab = [f"s{j}" for j in range(rng.randint(3, 8))]
        heads = rng.sample(["Inheritance", "Similarity"], rng.randint(1, 2))
        arity = rng.choice([2, 2, 2, 3])
        atoms = {"(" + " ".join([rng.choice(heads)] + [rng.choice(vocab) for _ in range(arity)]) + ")"
                 for _ in range(rng.randint(4, 24))}
        out.append({"name": f"random_{seed}_{i}", "edge_category": "boundary_minsup",
                    "rationale": "random baseline case", "atoms": sorted(atoms),
                    "minsup": rng.randint(2, 5), "depth": 0, "conj_exp": rng.random() < 0.5})
    return out


def random_cases(n, seed=0):
    accepted, _ = validate_all(random_proposals(n, seed))
    for c in accepted:
        c["edge_category"] = "random"
    return accepted[:n]
