"""Editorial plant taxonomy for the migrated catalog.

The legacy ``category`` value is often an SEO keyword list (and occasionally
incorrect). Classify by the plant name, using the botanical category for the
few ambiguous names. ``Крупномеры`` is a size facet, not a plant family.
"""

GROUPS = (
    ("fruit", "Плодовые деревья"),
    ("berry", "Ягодные и орехоплодные"),
    ("conifer", "Хвойные растения"),
    ("decor-tree", "Декоративные деревья"),
    ("decor", "Декоративные кустарники"),
    ("vine", "Лианы"),
)
GROUP_LABELS = dict(GROUPS)


def classify(product):
    if product.get("group_override") in GROUP_LABELS and product.get("subgroup_override"):
        return product["group_override"], product["subgroup_override"]
    name = product["name"].casefold()
    legacy_category = product.get("category", "").casefold()
    latin = product.get("characteristics", {}).get("Латинское название", "").casefold()

    # Named cultivars grown primarily for ornamental flowering or foliage.
    if name.startswith("яблон") and any(word in name for word in
           ("ола", "роялти", "недзвецкого", "ред сентинел", "рудольф")):
        return "decor-tree", "Декоративные яблони"
    if name.startswith("рябина") and "додонг" in name:
        return "decor-tree", "Рябины"
    if name.startswith("калина") and "бульденеж" in name:
        return "decor", "Калины"

    if name.startswith("актинидия"):
        return "vine", "Актинидии"

    fruit = (
        ("абрикос", "Абрикосы"), ("алыча", "Алыча"), ("черешня", "Черешни"),
        ("черевишня", "Черевишни"), ("вишня", "Вишни"),
        ("дерево-сад", "Деревья-сад"), ("дерево - сад", "Деревья-сад"),
        ("груша", "Груши"), ("слива", "Сливы"), ("шарафуга", "Шарафуга"),
        ("шелковица", "Шелковицы"), ("яблоня", "Яблони"), ("яблоня\"", "Яблони"),
        ("рябина", "Рябины"),
    )
    for prefix, subgroup in fruit:
        if name.startswith(prefix):
            return "fruit", subgroup

    berry = (
        ("арония", "Аронии"), ("брусника", "Брусника"),
        ("черная смородина", "Смородина"), ("смородина", "Смородина"),
        ("голубика", "Голубика"), ("ирга", "Ирга"),
        ("крыжовник", "Крыжовник"), ("малина", "Малина"),
        ("облепиха", "Облепиха"), ("жимолость", "Жимолость"),
        ("лещина", "Лещина"), ("бузина", "Бузина"), ("калина", "Калина"),
    )
    for prefix, subgroup in berry:
        if name.startswith(prefix):
            return "berry", subgroup

    conifer = (
        ("ель", "Ели"), ("пихта", "Пихты"), ("сосна", "Сосны"),
        ("туя", "Туи"), ("лиственница", "Лиственницы"),
        ("можжевельник", "Можжевельники"), ("тис", "Тисы"),
    )
    for prefix, subgroup in conifer:
        if name.startswith(prefix):
            return "conifer", subgroup
    if name == "дискус" and ("пихта" in legacy_category or "abies" in latin):
        return "conifer", "Пихты"

    decor_tree = (
        ("береза", "Берёзы"), ("боярышник", "Боярышники"),
        ("ива", "Ивы"), ("каштан", "Каштаны"), ("клен", "Клёны"),
        ("миндаль", "Декоративный миндаль"),
    )
    for prefix, subgroup in decor_tree:
        if name.startswith(prefix):
            return "decor-tree", subgroup

    decor_shrub = (
        ("бересклет", "Бересклеты"), ("гортензия", "Гортензии"),
        ("пузыреплодник", "Пузыреплодники"), ("рододендрон", "Рододендроны"),
        ("сирень", "Сирень"), ("спирея", "Спиреи"),
        ("жасмин", "Чубушники"), ("чубушник", "Чубушники"),
    )
    for prefix, subgroup in decor_shrub:
        if name.startswith(prefix):
            return "decor", subgroup

    raise ValueError(f"Растение без группы: {product['name']} ({product.get('source_url', '')})")


def is_large_specimen(product):
    return "крупномер" in product.get("category", "").casefold()
