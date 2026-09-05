from ul_house.models.combat_mechanism import Element

ELEMENT = {
    "fire": Element(
        id="fire",
        effective=("wind",),
        weakness=("water",),
    ),
    "water": Element(
        id="water",
        effective=("fire",),
        weakness=("wind",),
    ),
    "wind": Element(
        id="wind",
        effective=("water",),
        weakness=("fire",),
    ),
    "light": Element(
        id="light",
        effective=("dark", "time"),
        weakness=("dark", "star"),
    ),
    "dark": Element(
        id="dark",
        effective=("light", "time"),
        weakness=("light", "star"),
    ),
    "time": Element(
        id="time",
        effective=("star",),
        weakness=("light", "dark"),
    ),
    "star": Element(
        id="star",
        effective=("light", "dark"),
        weakness=("time",),
    ),
}
