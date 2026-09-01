from ul_house.models.element import Element

ELEMENT = {
    Element(
        id="fire",
        effective=("wind",),
        weakness=("water",),
    ),
    Element(
        id="water",
        effective=("fire",),
        weakness=("wind",),
    ),
    Element(
        id="wind",
        effective=("water",),
        weakness=("fire",),
    ),
    Element(
        id="light",
        effective=("dark", "time"),
        weakness=("dark", "star"),
    ),
    Element(
        id="dark",
        effective=("light", "time"),
        weakness=("light", "star"),
    ),
    Element(
        id="time",
        effective=("star",),
        weakness=("light", "dark"),
    ),
    Element(
        id="star",
        effective=("light", "dark"),
        weakness=("time",),
    ),
}
