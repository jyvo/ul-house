from ul_house.data.elements import ELEMENT
from ul_house.models.equipment import ItemRef, EquipmentRef, Weapon, DefensiveGear, Monster, Stats
from ul_house.models.gear_evolution import Reforge, Awakening, Enlightening, GearEvoMaterial, ItemEvoMaterial
from ul_house.models.gear_mechanism import Proc, ProcActivation, ProcScaling, SkillEffect, WeaponAbility, MonsterSkill, PassiveSkill, HiddenPotential, PotentialLevel


UR_WEAPON = Weapon(
    uid="1015655",
    name="[surging sea] sea dragon's sword",
    rarity="ur",
    gear_type="sword",
    cost=44,
    element=ELEMENT["water"],
    max_level=120,
    stats=(
        Stats(
            label="atk",
            values=(
                ("initial", 10111),
                ("max1", 32741),
                ("max2", 72097)
            )
        ),
        Stats(
            label="matk",
            values=(
                ("initial", 3740),
                ("max1", 12110),
                ("max2", 26666)
            )
        )
    ),
    reforge=Reforge(
        before=EquipmentRef(
            uid="1014667",
            name="sea dragon's sword",
        )
    ),
    infusion_count=5,
    skill=Proc(
        name="water dragon slayer xl",
        effect=(
            SkillEffect(
                description="increases physical and magic ability power by 4xl+."
            ),
            SkillEffect(
                description="increases damage when using a physical or magic attack ability by s+."
            ),
            SkillEffect(
                target="yourself",
                description="recovers 10 cost."
            ),
            SkillEffect(
                description="increases physical and magic attack ability success value by m."
            )
        ),
        activation=ProcActivation(
            condition=("when a physical or magic attack ability is used.",),
            rate="l",
        ),
        scaling=(
            ProcScaling(
                description="ability power scales with the number of water elemental weapons equipped up to a max of 5xl+ (gear pieces needed for maximum effect: 5)."
            ),
            ProcScaling(
                description="activation rate scales with the number of water elemental weapons equipped up to a max of xl (gear pieces needed for maximum effect: 5)."
            ),
        )
    ),
    weapon_ability=WeaponAbility(
        name="valiant blade",
        effect=(
            SkillEffect(
                target="1 enemy",
                description="ability power 110. deals physical damage 1 time that penetrates damage reduction and damage reflection effects with a success value of 100."
            ),
            SkillEffect(
                target="yourself",
                description="fills unison gauge by 4."
            )
        ),
        uid="2130"
    )
)

XENO_WEAPON = Weapon(
    uid="1015157",
    name='absolute xenoblade',
    rarity="ur",
    gear_type="sword",
    cost=40,
    element=ELEMENT["water"],
    max_level=70,
    stats=(
        Stats(
            label="atk",
            values=(
                ("initial", 6607),
                ("max", 22886),
            )
        ),
        Stats(
            label="matk",
            values=(
                ("initial", 2444),
                ("max", 8465),
            )
        )
    ),
    awakening=Awakening(
        before=EquipmentRef(
            uid="1015065",
            name="xenoblade exceed",
        ),
        after=EquipmentRef(
            uid="1015615",
            name="jewel bright xenoblade",
        ),
        gear_materials=(
            GearEvoMaterial(
                gear=EquipmentRef(
                    uid="1015171",
                    name="sapphire dragon sword",
                )
            ),
            GearEvoMaterial(
                gear=EquipmentRef(
                    uid="1015083",
                    name="glimmering fire crystal sword",
                )
            ),
            GearEvoMaterial(
                gear=EquipmentRef(
                    uid="1015077",
                    name="shotel of nox",
                )
            ),
            GearEvoMaterial(
                gear=EquipmentRef(
                    uid="1015076",
                    name="morning glory sword",
                )
            ),
            GearEvoMaterial(
                gear=EquipmentRef(
                    uid="1015086",
                    name="azrulmalik sayf",
                )
            )
        ),
        item_materials=(
            ItemEvoMaterial(
                quantity=100,
                item=ItemRef(
                    uid="5864",
                    name="sword slate",
                )
            ),
            ItemEvoMaterial(
                quantity=150,
                item=ItemRef(
                    uid="7043",
                    name="emerald medal",
                )
            ),
            ItemEvoMaterial(
                quantity=1000,
                item=ItemRef(
                    uid="3077",
                    name="ice beast horn",
                )
            ),
            ItemEvoMaterial(
                quantity=30,
                item=ItemRef(
                    uid="80129",
                    name="avsaris' claw",
                )
            ),
            ItemEvoMaterial(
                quantity=20,
                item=ItemRef(
                    uid="80135",
                    name="wind spirit lamp"
                )
            )
        )
    ),
    infusion_count=0,
    skill=Proc(
        name="patriot sword: infinity",
        effect=(
            SkillEffect(
                description="atk & matk of swords/axes +25%"
            ),
        ),
    )
)

MON_PASSIVE = Monster(
    uid="1796604",
    name="[eclipse's hope] p. valkyrie nikyt",
    rarity="ur",
    gear_type="monster",
    cost=45,
    element=ELEMENT["dark"],
    max_level=100,
    stats=(
        Stats(
            label="stats1",
            values=(
                ("initial", 3175),
                ("max1", 11751),
                ("max2", 12086),
            )
        ),
        Stats(
            label="stats2",
            values=(
                ("initial", 3175),
                ("max1", 11751),
                ("max2", 12086),
            )
        )
    ),
    reforge=Reforge(
        before=EquipmentRef(
            uid="1796603",
            name="paradox valkyrie nikyt",
        )
    ),
    skill=(
        MonsterSkill(
            name="punishment by the black flame",
            effect=(
                SkillEffect(
                    target="all enemies",
                    description="ability power 1100. success value 100. 2x physical damage (dark)."
                ),
                SkillEffect(
                    description="ability power boosted by 10 for each increase in skill level."
                ),
                SkillEffect(
                    target="all allies",
                    description="increases all stats by 200% for 150 seconds."
                ),
                SkillEffect(
                    target="all allies",
                    description="increases buff limit for all stats by 120% for 150 seconds."
                ),
                SkillEffect(
                    target="all allies",
                    description="increases physical and magic attack ability power by 100 for 150 seconds."
                ),
                SkillEffect(
                    target="all allies",
                    description="boosts critical hit ability power by 100 for 150 seconds."
                ),
                SkillEffect(
                    target="all allies",
                    description="increases physical and magic ability power and critical assault buff limits by 50 for 150 seconds."
                ),
                SkillEffect(
                    target="all allies",
                    description="applies blessing (recovers 50 cost, increases max hp by 100%, nullifies status ailments and unison gauge reduction effects, reduces cooldown times by 50%, and recovers 20% of max hp every 5 seconds) for 150 seconds."
                ),
                SkillEffect(
                    target="yourself",
                    description="applies flank for 150 seconds (when in effect, damage to hp and cp will not be taken; [special] increases all stats by 100%; [special] ignores 50% of the target's def and mdef; [special] permits piercing through damage reduction, damage reflection, and damage nullifying effects; nullifies cost reduction and unison gauge reduction: removed after receiving a set amount of damage (300% of max hp excluding buff and field effects))."
                )
            )
        ),
    ),
    passive=PassiveSkill(
        name="eclipse's blessing",
        effect=(
            SkillEffect(
                description="status ailment resistance boosted by 45 when equipped in the main slot."
            ),
        )
    ),
    hidden_potential=HiddenPotential(
        unlocks=(
            PotentialLevel(
                level=1,
                effect=SkillEffect(
                    description="increases stats selected in stat slot 1 and stat slot 2 by 40000"
                )
            ),
            PotentialLevel(
                level=2,
                effect=SkillEffect(
                    description="[additional unison effect] target: all allies. increases unison gauge gains by 2 for 150 seconds."
                )
            ),
            PotentialLevel(
                level=3,
                effect=SkillEffect(
                    description="rejection resistance +60."
                )
            ),
            PotentialLevel(
                level=4,
                effect=SkillEffect(
                    description="light and dark defense from unison damage +700"
                )
            )
        )
    )
)

FATEWOVEN_MON = Monster(
    uid="1500502",
    name='[raging howl fatewoven] fermuraze',
    rarity="ur",
    gear_type="monster",
    cost=45,
    element=ELEMENT["fire"],
    max_level=100,
    stats=(
        Stats(
            label="stats1",
            values=(
                ("initial", 3175),
                ("max1", 11751),
                ("max2", 12086)
            )
        ),
        Stats(
            label="stats2",
            values=(
                ("initial", 3175),
                ("max1", 11751),
                ("max2", 12086)
            )
        )
    ),
    reforge=Reforge(
        before=EquipmentRef(
            uid="1500501",
            name="fermuraze, keeper of the chaotic flame"
        )
    ),
    skill=(
        MonsterSkill(
            name="dormant mystical anima fermuraze",
            effect=(
                SkillEffect(
                    target="all enemies",
                    description="ability power 500. success value 100. 5x magic damage (fire)."
                ),
                SkillEffect(
                    description="ability power boosted by 10 for each increase in skill level."
                ),
                SkillEffect(
                    target="all allies",
                    description="increases all stats by 200% for 150 seconds."
                ),
                SkillEffect(
                    target="all allies",
                    description="increases buff limit for all stats by 120% for 150 seconds."
                ),
                SkillEffect(
                    target="all allies",
                    description="recovers 100 cost."
                ),
                SkillEffect(
                    target="all allies excluding yourself",
                    description="fills unison gauge by 50."
                ),
                SkillEffect(
                    target="all allies",
                    description="applies dawn for 150 seconds (increases max hp and all stats by 100%; nullifies cost reduction and unison gauge reduction; recovers 10 cost when using an ability. when in effect and an enemy is defeated, recovers 30% of your max hp; applies dual attack; and debuffs/special debuffs are removed)."
                ),
                SkillEffect(
                    target="yourself",
                    description="applies skirmish for 150 seconds (when in effect, damage to hp and cp will not be taken; [special] permits piercing through damage reduction, damage reflection, and damage nullifying effects; [special] fill your unison gauge by 3 and recover 5 cost when using an ability; [special] restores cp based on damage dealt. removed after receiving a set amount of damage (300% of max hp excluding buff and field effects))."
                )
            )
        ),
        MonsterSkill(
            name="dormant mystical anima fermuraze",
            effect=(
                SkillEffect(
                    description="during element battles when this unison effect is used, the value of the wind element weakening effect on monsters is increased by 10%."
                ),
                SkillEffect(
                    target="all enemies",
                    description="ability power 700. deal magic damage (fire) 3 times that penetrates damage reduction and damage reflection effects with a success value of 100."
                ),
                SkillEffect(
                    description="ability power boosted by 10 for each increase in skill level."
                ),
                SkillEffect(
                    target="all enemies",
                    description="removes 50 cost."
                ),
                SkillEffect(
                    target="all enemies",
                    description="reduces unison gauge by 50."
                ),
                SkillEffect(
                    target="all allies",
                    description="increases physical and magic attack ability power by 100 for 150 seconds."
                ),
                SkillEffect(
                    target="all allies",
                    description="boosts critical hit ability power by 100 for 150 seconds."
                ),
                SkillEffect(
                    target="all allies",
                    description="increases physical and magic ability power and critical assault buff limits by 50 for 150 seconds."
                ),
                SkillEffect(
                    target="all allies",
                    description="applies dawn for 150 seconds (increases max hp and all stats by 100%; nullifies cost reduction and unison gauge reduction; recovers 10 cost when using an ability. when in effect and an enemy is defeated, recovers 30% of your max hp; applies dual attack; and debuffs/special debuffs are removed)."
                ),
                SkillEffect(
                    target="yourself",
                    description="applies skirmish for 150 seconds (when in effect, damage to hp and cp will not be taken; [special] permits piercing through damage reduction, damage reflection, and damage nullifying effects; [special] fill your unison gauge by 3 and recover 5 cost when using an ability; [special] restores cp based on damage dealt. removed after receiving a set amount of damage (300% of max hp excluding buff and field effects))."
                )
            )
        )
    ),
    hidden_potential=HiddenPotential(
        unlocks=(
            PotentialLevel(
                level=1,
                effect=SkillEffect(
                    description="increases stats selected in stat slot 1 and stat slot 2 by 60000"
                )
            ),
            PotentialLevel(
                level=2,
                effect=SkillEffect(
                    description="scorching resistance +60."
                )
            ),
            PotentialLevel(
                level=3,
                effect=SkillEffect(
                    description="fire defense from unison damage +1000"
                )
            ),
            PotentialLevel(
                level=4,
                effect=SkillEffect(
                    description="paralysis and blind resistance up by 70"
                )
            )
        ),
        restrictions="[raging howl fatewoven] fermuraze or ninoyu with the same or higher cost of the base monster can be used as fodder. *specific monsters may also be used. see the help section for details."
    )
)

FATEWOVEN_GEAR = DefensiveGear(
    uid="1890424",
    name="[oblivion] john doe's hat (f)",
    rarity="ur",
    gear_type="hat",
    cost=45,
    element=ELEMENT["dark"],
    max_level=100,
    stats=(
        Stats(
            label="def",
            values=(
                ("initial", 3358),
                ("max1", 10638),
                ("max2", 24881)
            )
        ),
        Stats(
            label="mdef",
            values=(
                ("initial", 11242),
                ("max1", 35613),
                ("max2", 83295)
            )
        )
    ),
    reforge=Reforge(
        before=EquipmentRef(
            uid="1890423",
            name="john doe's hat (f)",
        )
    ),
    infusion_count=3,
    skill=Proc(
        name="mastery over dark xl",
        effect=(
            SkillEffect(
                description="reduces physical and magic damage taken by l+."
            ),
            SkillEffect(
                description="increases status ailment resistance by xxl."
            ),
            SkillEffect(
                description="reduces physical ability power by 4xl+."
            ),
            SkillEffect(
                description="reduces magic ability power by 4xl+."
            )
        ),
        activation=ProcActivation(
            condition=("when physical or magic damage is taken.",),
            rate="m+"
        ),
        scaling=(
            ProcScaling(
                description="damage reduction scales with the number of dark elemental helm/hat/armor/clothing type gear pieces equipped up to a max of xxl (gear pieces needed for maximum effect: 3)."
            ),
            ProcScaling(
                description="activation rate scales with the number of dark elemental helm/hat/armor/clothing type gear pieces equipped up to a max of xl (gear pieces needed for maximum effect: 3)."
            )
        )
    )
)

AWAKENING_MON = Monster(
    uid="4425111",
    name='[soldier founder] odinia',
    rarity="ur",
    gear_type="monster",
    cost=43,
    element=ELEMENT["water"],
    max_level=70,
    stats=(
        Stats(
            label="def",
            values=(
                ("initial", 2875),
                ("max", 10064)
            )
        ),
        Stats(
            label="mdef",
            values=(
                ("initial", 2875),
                ("max", 10064)
            )
        )
    ),
    awakening=Awakening(
        before=EquipmentRef(
            uid="4424110",
            name="[magic crystal seal] odinia"
        )
    ),
    skill=(
        MonsterSkill(
            name="patriot sword: infinity",
            effect=(
                SkillEffect(
                    target="all enemies",
                    description="ability power 400. physical damage (water)."
                ),
                SkillEffect(
                    description="ability power boosted by 100 for each increase in skill level."
                ),
                SkillEffect(
                    target="yourself",
                    description="applies hero for 60 seconds (increases max hp and all stats by 100%; nullifies debuffs, status ailments, and unison gauge reduction; nullifies buffs [demerit]; reduces healing effects received by 70% [demerit])."
                )
            )
        ),
    ),
    passive=PassiveSkill(
        name="eins - schwert",
        effect=(
            SkillEffect(
                description="if your class is a soldier class, reduces fire unison attack and great spirit unison attack damage by 90% when equipped in the main slot."
            ),
        )
    )
)

ENLIGHTENING_MON = Monster(
    uid="4435013",
    name='oberon, sky emperor',
    rarity="ur",
    gear_type="monster",
    cost=35,
    element=ELEMENT["wind"],
    max_level=100,
    stats=(
        Stats(
            label="stats1",
            values=(
                ("initial", 1675),
                ("max1", 6185),
                ("max2", 7165)
            )
        ),
        Stats(
            label="stats2",
            values=(
                ("initial", 1675),
                ("max1", 6185),
                ("max2", 7165)
            )
        )
    ),
    reforge=Reforge(
        before=EquipmentRef(
            uid="4434015",
            name="oberon"
        )
    ),
    skill=(
        MonsterSkill(
            name="translucent melody",
            effect=(
                SkillEffect(
                    description="ability power 220. damages all enemies with magic wind damage."
                ),
                SkillEffect(
                    description="ability power boosted by 10 for each increase in skill level."
                ),
                SkillEffect(
                    description="confuses all enemies for 60 seconds at success of 80 points."
                ),
                SkillEffect(
                    description="removes buffs from all enemies."
                )
            )
        ),
    ),
    hidden_potential=HiddenPotential(
        unlocks=(
            PotentialLevel(
                level=1,
                effect=SkillEffect(
                    description="increases wind elemental attack by 1"
                )
            ),
            PotentialLevel(
                level=2,
                effect=SkillEffect(
                    description="[additional unison effect] increases ability power of wind great spirit unison attack by 20 if summoned using this unison skill."
                )
            ),
            PotentialLevel(
                level=3,
                effect=SkillEffect(
                    description="wind defense from unison damage + 120"
                )
            ),
            PotentialLevel(
                level=4,
                effect=SkillEffect(
                    description="[unison effect boost] increases the ability power of the 220-ability-power attack that deals magic wind damage to all enemies by 100."
                )
            ),
            PotentialLevel(
                level=5,
                effect=SkillEffect(
                    description="[additional unison effect] water weakening effect in the element battle is boosted by 10% when this unison skill is used."
                )
            )
        )
    ),
    enlightening=Enlightening(
        after=EquipmentRef(
            uid="4435353",
            name="[emperor of mystic zephyr] oberon"
        ),
        gear_materials=(
            GearEvoMaterial(
                quantity=2,
                gear=EquipmentRef(
                    uid="1104903",
                    name="emerald spirit orb"
                )
            ),
            GearEvoMaterial(
                quantity=2,
                gear=EquipmentRef(
                    uid="4434901",
                    name="green passa king"
                )
            ),
            GearEvoMaterial(
                gear=EquipmentRef(
                    uid="1075190",
                    name="[wind emperor's treasure] maestria"
                )
            )
        ),
        item_materials=(
            ItemEvoMaterial(
                quantity=5,
                item=ItemRef(
                    uid="7007",
                    name="large jewel of enlightenment"
                )
            ),
            ItemEvoMaterial(
                quantity=50,
                item=ItemRef(
                    uid="7010",
                    name="aromatic orb of enlightenment"
                )
            ),
            ItemEvoMaterial(
                quantity=5,
                item=ItemRef(
                    uid="3019",
                    name="aero emerald"
                )
            ),
            ItemEvoMaterial(
                quantity=80,
                item=ItemRef(
                    uid="5817",
                    name="verdure leaf"
                )
            ),
            ItemEvoMaterial(
                quantity=50,
                item=ItemRef(
                    uid="3004",
                    name="wind medal"
                )
            )
        )
    )
)

