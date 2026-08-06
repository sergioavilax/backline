"""B0 packer unit tests (keyless): relevance ordering, budget cap, determinism."""

from evals.baselines import CorpusIndex, _CorpusFile, pack_context


def _index() -> CorpusIndex:
    contracts = [
        _CorpusFile(
            name="FBR-C-00501_nova-reyes.txt",
            content="RECORDING AGREEMENT between Nova Reyes and Foldback Records. "
            "Royalties on streaming revenue shall be thirty percent." * 3,
            tokens=60,
        ),
        _CorpusFile(
            name="FBR-C-00502_kaiya-marsh.txt",
            content="RECORDING AGREEMENT between Kaiya Marsh and Foldback Records." * 3,
            tokens=50,
        ),
        _CorpusFile(
            name="FBR-C-00503_gentle-paradox.txt",
            content="RECORDING AGREEMENT between Gentle Paradox and the label." * 3,
            tokens=50,
        ),
    ]
    statements = [
        _CorpusFile(name="kinetic_digital_2026-03.csv", content="isrc,units\nAA,1", tokens=8),
        _CorpusFile(name="kinetic_digital_2026-04.csv", content="isrc,units\nBB,2", tokens=8),
    ]
    return CorpusIndex(contracts=contracts, statements=statements)


def test_pack_prefers_named_artist_and_period() -> None:
    result = pack_context(
        _index(), "What royalty rate does Nova Reyes earn on streaming in 2026-03?", 200
    )
    names = [name for name, _ in result.files]
    assert names[0] in {"FBR-C-00501_nova-reyes.txt", "kinetic_digital_2026-03.csv"}
    assert "FBR-C-00501_nova-reyes.txt" in names
    assert "kinetic_digital_2026-03.csv" in names
    # The other period's drop scored lower than the named one.
    if "kinetic_digital_2026-04.csv" in names:
        assert names.index("kinetic_digital_2026-03.csv") < names.index(
            "kinetic_digital_2026-04.csv"
        )
    assert result.packed_tokens <= 200


def test_pack_respects_token_budget() -> None:
    result = pack_context(_index(), "Nova Reyes streaming royalties", 65)
    assert result.packed_tokens <= 65
    assert len(result.files) >= 1  # the top file fits


def test_pack_is_deterministic() -> None:
    a = pack_context(_index(), "Kaiya Marsh downloads", 500)
    b = pack_context(_index(), "Kaiya Marsh downloads", 500)
    assert a == b


def test_pack_wraps_material_as_data() -> None:
    result = pack_context(_index(), "Nova Reyes", 500)
    assert result.text.startswith("<materials file=")
    assert "</materials>" in result.text
