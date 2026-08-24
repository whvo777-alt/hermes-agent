"""Build prompts for data-bearing body infographics."""

import re


_CATEGORY_CONTEXT = {
    "health": "한국 건강·웰니스 블로그용",
    "finance": "한국 재테크·금융 블로그용",
    "it-tech": "한국 IT·기술 블로그용",
    "self-dev": "한국 자기계발 블로그용",
}

_CATEGORY_RULES = {
    "health": (
        "병원 광고가 아니라 건강 정보 콘텐츠 분위기로 만든다.",
        "병을 단정하거나 스스로 진단하게 만드는 글자를 넣지 않는다.",
        "확인 · 점검 · 상담 고려 같은 말을 쓴다.",
        "진한 빨강, 무섭게 느껴지는 그림, 아픈 부위 사진을 넣지 않는다.",
        "주의가 필요한 곳에만 낮은 채도의 주황이나 코랄을 쓴다.",
    ),
    "self-dev": (
        "성공학 광고가 아니라 실용적인 생산성 자료 분위기로 만든다.",
        "돈다발, 트로피, 로켓, 슈퍼히어로 그림을 넣지 않는다.",
        "무조건 성공 · 인생이 바뀐다 같은 부풀린 글자를 넣지 않는다.",
        "강조가 필요한 곳에만 낮은 채도의 옐로우나 오렌지를 쓴다.",
        "실제로 할 수 있는 행동에 초점을 맞춘다.",
    ),
}

_STYLE_KIND = {
    "checklist": "checklist",
    "grid": "summary",
    "timeline": "timeline",
    "ordered": "timeline",
    "bullets": "checklist",
    "before_after": "before_after",
    "qa": "qa",
    "risk_tier": "risk",
    "ox_quiz": "ox",
    "gauge": "gauge",
    "quote": "quote",
    "quote_keyword": "quote",
}


def build_infographic_prompt(spec, *, category_id: str = "", category_name: str = "") -> str:
    """본문에서 뽑은 재료로 인포그래픽 프롬프트를 만든다."""
    if spec is None:
        return ""

    title = str(getattr(spec, "display_title", "") or getattr(spec, "heading", "") or "")
    title = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", title)
    title = re.sub(r"\*\*(.*?)\*\*", r"\1", title)
    title = re.sub(r"`([^`]*)`", r"\1", title)
    title = re.sub(r"^\s*(?:[-*]\s+|\d+\.\s+)", "", title).strip()
    title = re.sub(r"\s+", " ", title)
    if len(title) > 45:
        title_cut = title[:45]
        title = title_cut.rsplit(" ", 1)[0] if " " in title_cut else title_cut
    if not title:
        return ""

    items = []
    for raw_item in list(getattr(spec, "items", ()) or ()):
        if len(items) >= 6:
            break
        item = str(raw_item or "").strip()
        item = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", item)
        item = re.sub(r"\*\*(.*?)\*\*", r"\1", item)
        item = re.sub(r"`([^`]*)`", r"\1", item)
        item = re.sub(r"^\s*(?:[-*]\s+|\d+\.\s+)", "", item).strip()
        item = re.sub(r"\s+", " ", item)
        if len(item) > 45:
            item_cut = item[:45]
            item = item_cut.rsplit(" ", 1)[0] if " " in item_cut else item_cut
        if item:
            items.append(item)

    table = []
    raw_table = getattr(spec, "table", None)
    if raw_table:
        for raw_row in list(raw_table):
            row = []
            for raw_cell in list(raw_row or ()):
                cell = str(raw_cell or "").strip()
                cell = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cell)
                cell = re.sub(r"\*\*(.*?)\*\*", r"\1", cell)
                cell = re.sub(r"`([^`]*)`", r"\1", cell)
                cell = re.sub(r"^\s*(?:[-*]\s+|\d+\.\s+)", "", cell).strip()
                cell = re.sub(r"\s+", " ", cell)
                if len(cell) > 45:
                    cell_cut = cell[:45]
                    cell = cell_cut.rsplit(" ", 1)[0] if " " in cell_cut else cell_cut
                row.append(cell)
            if row:
                table.append(row)

    qa_pairs = []
    for raw_pair in list(getattr(spec, "qa_pairs", ()) or ())[:2]:
        if len(raw_pair) < 2:
            continue
        pair = []
        for raw_value in raw_pair[:2]:
            value = str(raw_value or "").strip()
            value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
            value = re.sub(r"\*\*(.*?)\*\*", r"\1", value)
            value = re.sub(r"`([^`]*)`", r"\1", value)
            value = re.sub(r"^\s*(?:[-*]\s+|\d+\.\s+)", "", value).strip()
            value = re.sub(r"\s+", " ", value)
            if len(value) > 45:
                value_cut = value[:45]
                value = value_cut.rsplit(" ", 1)[0] if " " in value_cut else value_cut
            pair.append(value)
        if pair[0] and pair[1]:
            qa_pairs.append((pair[0], pair[1]))

    before_pairs = []
    for raw_pair in list(getattr(spec, "before_pairs", ()) or ())[:4]:
        if len(raw_pair) < 2:
            continue
        pair = []
        for raw_value in raw_pair[:2]:
            value = str(raw_value or "").strip()
            value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
            value = re.sub(r"\*\*(.*?)\*\*", r"\1", value)
            value = re.sub(r"`([^`]*)`", r"\1", value)
            value = re.sub(r"^\s*(?:[-*]\s+|\d+\.\s+)", "", value).strip()
            value = re.sub(r"\s+", " ", value)
            if len(value) > 45:
                value_cut = value[:45]
                value = value_cut.rsplit(" ", 1)[0] if " " in value_cut else value_cut
            pair.append(value)
        if pair[0] and pair[1]:
            before_pairs.append((pair[0], pair[1]))

    risk_tiers = []
    for raw_tier in list(getattr(spec, "risk_tiers", ()) or ())[:3]:
        if len(raw_tier) < 3:
            continue
        tier_values = []
        for raw_value in raw_tier[:3]:
            value = str(raw_value or "").strip()
            value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
            value = re.sub(r"\*\*(.*?)\*\*", r"\1", value)
            value = re.sub(r"`([^`]*)`", r"\1", value)
            value = re.sub(r"^\s*(?:[-*]\s+|\d+\.\s+)", "", value).strip()
            value = re.sub(r"\s+", " ", value)
            if len(value) > 45:
                value_cut = value[:45]
                value = value_cut.rsplit(" ", 1)[0] if " " in value_cut else value_cut
            tier_values.append(value)
        if tier_values[1] and tier_values[2]:
            risk_tiers.append((tier_values[0], tier_values[1], tier_values[2]))

    quote_text = str(getattr(spec, "quote_text", "") or "").strip()
    quote_text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", quote_text)
    quote_text = re.sub(r"\*\*(.*?)\*\*", r"\1", quote_text)
    quote_text = re.sub(r"`([^`]*)`", r"\1", quote_text)
    quote_text = re.sub(r"^\s*(?:[-*]\s+|\d+\.\s+)", "", quote_text).strip()
    quote_text = re.sub(r"\s+", " ", quote_text)
    if len(quote_text) > 45:
        quote_cut = quote_text[:45]
        quote_text = quote_cut.rsplit(" ", 1)[0] if " " in quote_cut else quote_cut

    gauge_stat = getattr(spec, "gauge_stat", None)
    gauge_number = ""
    gauge_value = ""
    if gauge_stat and len(gauge_stat) >= 2:
        gauge_number = str(gauge_stat[0] or "").strip()
        gauge_unit = str(gauge_stat[1] or "").strip()
        gauge_value = f"{gauge_number}{gauge_unit}"
        if len(gauge_value) > 45:
            gauge_cut = gauge_value[:45]
            gauge_value = gauge_cut.rsplit(" ", 1)[0] if " " in gauge_cut else gauge_cut
    gauge_label = str(getattr(spec, "gauge_label", "") or "").strip()
    gauge_label = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", gauge_label)
    gauge_label = re.sub(r"\*\*(.*?)\*\*", r"\1", gauge_label)
    gauge_label = re.sub(r"`([^`]*)`", r"\1", gauge_label)
    gauge_label = re.sub(r"^\s*(?:[-*]\s+|\d+\.\s+)", "", gauge_label).strip()
    gauge_label = re.sub(r"\s+", " ", gauge_label)
    if len(gauge_label) > 45:
        gauge_label_cut = gauge_label[:45]
        gauge_label = (
            gauge_label_cut.rsplit(" ", 1)[0]
            if " " in gauge_label_cut
            else gauge_label_cut
        )

    ox_pair = getattr(spec, "ox_pair", None)
    ox_values = []
    if ox_pair and len(ox_pair) >= 2:
        for raw_value in ox_pair[:2]:
            value = str(raw_value or "").strip()
            value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
            value = re.sub(r"\*\*(.*?)\*\*", r"\1", value)
            value = re.sub(r"`([^`]*)`", r"\1", value)
            value = re.sub(r"^\s*(?:[-*]\s+|\d+\.\s+)", "", value).strip()
            value = re.sub(r"\s+", " ", value)
            if len(value) > 45:
                value_cut = value[:45]
                value = value_cut.rsplit(" ", 1)[0] if " " in value_cut else value_cut
            ox_values.append(value)

    selected_style = str(getattr(spec, "style", "") or "").strip()
    if not selected_style:
        selected_style = str(getattr(spec, "shape", "") or "").strip()
    kind = _STYLE_KIND.get(selected_style, "summary")
    if selected_style == "grid" and table:
        kind = "table"

    if kind in {"checklist", "summary", "timeline"} and len(items) < 2:
        return ""
    if kind == "before_after" and len(before_pairs) < 1:
        return ""
    if kind == "table" and (len(table) < 2 or not table[0]):
        return ""
    if kind == "qa" and len(qa_pairs) < 1:
        return ""
    if kind == "risk" and len(risk_tiers) < 1:
        return ""
    if kind == "ox" and len(ox_values) < 2:
        return ""
    if kind == "gauge" and (not gauge_number or not gauge_label):
        return ""
    if kind == "quote" and not quote_text:
        return ""

    category_context = _CATEGORY_CONTEXT.get(category_id, "한국 생활정보 블로그용")
    prompt_lines = [
        category_context + ".",
        "밝은 아이보리 배경, 딥 네이비 제목, 블루와 민트 포인트 색.",
        "세로로 긴 4:5 비율. 1080 x 1350 픽셀.",
        "작고 단순한 플랫 벡터 아이콘을 쓴다.",
        "충분한 여백과 둥근 모서리 카드.",
        "모바일에서 읽기 쉬운 큰 글씨.",
        "사람 얼굴을 크게 넣지 않는다. 로고와 워터마크를 넣지 않는다.",
        "모든 글자는 한국어로 쓴다. 영어 낱말을 섞지 않는다.",
        "따옴표 안의 문장은 글자 하나 바꾸지 말고 그대로 쓴다.",
        "따옴표 표시 자체는 그리지 않는다.",
        "없는 문장을 지어내지 않는다.",
        f'제목은 "{title}".',
    ]
    prompt_lines = [line for line in prompt_lines if line]
    prompt_lines.extend(_CATEGORY_RULES.get(category_id, ()))

    if kind == "checklist":
        prompt_lines.extend(
            [
                f'제목 아래에 큰 체크박스 {len(items)}개를 세로로 나란히 배치한다.',
                "각 체크박스 오른쪽에 아래 문장을 하나씩 넣는다.",
            ]
        )
        prompt_lines.extend(f'"{item}"' for item in items)
    elif kind == "summary":
        prompt_lines.extend(
            [
                f'제목 아래에 둥근 카드 {len(items)}개를 2열로 배치한다.',
                "각 카드에 작은 아이콘 하나와 아래 문장을 하나씩 넣는다.",
            ]
        )
        prompt_lines.extend(f'"{item}"' for item in items)
    elif kind == "timeline":
        prompt_lines.extend(
            [
                "제목 아래에 위에서 아래로 이어지는 흐름선을 그리고",
                f"번호가 붙은 동그라미 {len(items)}개를 순서대로 놓는다.",
                "각 동그라미 오른쪽에 아래 문장을 순서대로 하나씩 넣는다.",
            ]
        )
        prompt_lines.extend(f'"{item}"' for item in items)
    elif kind == "before_after":
        prompt_lines.extend(
            [
                "제목 아래에 좌우 두 칸을 만든다.",
                '왼쪽 칸 머리글은 "이전", 오른쪽 칸 머리글은 "이후".',
                "같은 줄에 짝을 맞춰 아래 내용을 넣는다.",
            ]
        )
        prompt_lines.extend(f'이전: "{before}" / 이후: "{after}"' for before, after in before_pairs)
    elif kind == "table":
        header = table[0]
        prompt_lines.extend(
            [
                f'제목 아래에 {len(header)}칸 표를 그린다.',
                f'표의 머리줄은 {", ".join(f'"{cell}"' for cell in header)}.',
                "아래 줄들을 순서대로 넣는다.",
            ]
        )
        prompt_lines.extend(" | ".join(f'"{cell}"' for cell in row) for row in table[1:5])
    elif kind == "qa":
        prompt_lines.extend(
            [
                "문답 하나를 카드 하나로 만든다.",
                "카드 위쪽에 질문, 카드 아래쪽에 답을 넣는다.",
                "카드들을 같은 크기로 세로로 나란히 배치한다.",
            ]
        )
        prompt_lines.extend(f'질문: "{question}" 답: "{answer}"' for question, answer in qa_pairs)
    elif kind == "risk":
        prompt_lines.extend(
            [
                "제목 아래에 신호등처럼 위에서 아래로 세 단을 그린다.",
                "위는 민트, 가운데는 낮은 채도의 주황, 아래는 낮은 채도의 코랄.",
                "공포감을 주는 진한 빨강을 쓰지 않는다. 차분하게 그린다.",
            ]
        )
        prompt_lines.extend(
            f'이름표: "{label}" 설명: "{description}"'
            for _, label, description in risk_tiers
        )
    elif kind == "ox":
        prompt_lines.extend(
            [
                "제목 아래를 세로로 나눠 왼쪽에 큰 O, 오른쪽에 큰 X를 그린다.",
                f'O: "{ox_values[0]}"',
                f'X: "{ox_values[1]}"',
            ]
        )
    elif kind == "gauge":
        prompt_lines.extend(
            [
                f'제목 아래 가운데에 아주 큰 숫자 "{gauge_value}"을 놓는다.',
                f'숫자 둘레에 반원 눈금을 그리고, 숫자 아래에 "{gauge_label}"을 작게 쓴다.',
            ]
        )
    elif kind == "quote":
        prompt_lines.extend(
            [
                "제목 아래 가운데에 큰 따옴표 모양 장식을 하나 놓고",
                f'그 아래에 "{quote_text}" 한 문장만 크게 쓴다. 다른 글자는 넣지 않는다.',
            ]
        )

    return "\n".join(prompt_lines)
