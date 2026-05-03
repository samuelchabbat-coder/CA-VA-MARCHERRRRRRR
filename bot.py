import discord
from discord.ext import commands
import json, os, asyncio, random

# ══════════════════════════════════════════════════════
#  ⚙️  CONFIG — MODIFIE CES VALEURS
# ══════════════════════════════════════════════════════
GUILD_ID          = 1162542486779072664   # ID de ton serveur
CATEGORY_ID       = 1500100508453441736   # Catégorie pour les channels de match
QUEUE_VC_ID       = 1500100693028110406   # Vocal queue permanent
LOBBY_VC_ID       = 1500112332163121262   # Vocal lobby intermédiaire (créé par toi, permanent)
LEADERBOARD_CH_ID = 1500112197827825694   # Channel #leaderboard
TOKEN             = os.getenv("TOKEN", "")   # Mets ton token dans Variables sur Railway

QUEUE_SIZE    = 6      # 3v3 = 6 joueurs
LOBBY_WAIT    = 15     # secondes d'attente dans le lobby avant de lancer la draft
ELO_DEFAULT   = 1000
ELO_K         = 32
VOTE_TIMEOUT  = 600    # 10 min pour voter le résultat
CLEANUP_DELAY = 15     # secondes avant suppression des channels
VETO_TIMEOUT  = 60     # secondes pour veto les caps

DATA_FILE = "data.json"

# Paliers grades : (elo_min, label, couleur_discord, nom_role)
GRADES = [
    (1500, "⬛ Master",   discord.Color.from_str("#B9F2FF"), "Master"),
    (1400, "💎 Diamond",  discord.Color.from_str("#00BFFF"), "Diamond"),
    (1300, "💚 Emerald",  discord.Color.from_str("#50C878"), "Emerald"),
    (1200, "🩶 Platinum", discord.Color.from_str("#E5E4E2"), "Platinum"),
    (1100, "🥇 Gold",     discord.Color.from_str("#FFD700"), "Gold"),
    (0,    "⬜ Silver",   discord.Color.from_str("#C0C0C0"), "Silver"),
]

def get_grade(elo: int):
    for threshold, label, color, role_name in GRADES:
        if elo >= threshold:
            return label, color, role_name
    return "⬜ Silver", discord.Color.from_str("#C0C0C0"), "Silver"

# ══════════════════════════════════════════════════════
#  💾  PERSISTANCE JSON
# ══════════════════════════════════════════════════════
def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"players": {}, "match_counter": 0}
    with open(DATA_FILE) as f:
        return json.load(f)

def save_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_player(data: dict, uid: int) -> dict:
    k = str(uid)
    if k not in data["players"]:
        data["players"][k] = {
            "elo": ELO_DEFAULT,
            "wins": 0,
            "losses": 0,
            "pending_vote": None
        }
    return data["players"][k]

def elo_calc(winner_elo: float, loser_elo: float):
    exp_w = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    delta_w = round(ELO_K * (1 - exp_w))
    delta_l = round(ELO_K * (0 - (1 - exp_w)))
    return delta_w, delta_l

# ══════════════════════════════════════════════════════
#  🤖  BOT INIT
# ══════════════════════════════════════════════════════
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

queue: list[int] = []
active_matches: dict[int, dict] = {}

# ══════════════════════════════════════════════════════
#  🗳️  VIEW : VETO CAPITAINES
# ══════════════════════════════════════════════════════
class VetoView(discord.ui.View):
    def __init__(self, match_id: int, players: list[int], cap1: int, cap2: int):
        super().__init__(timeout=VETO_TIMEOUT)
        self.match_id  = match_id
        self.players   = players
        self.cap1      = cap1
        self.cap2      = cap2
        self.vetos     = set()
        self.accepts   = set()
        self.resolved  = False

    def _majority(self):
        return (len(self.players) // 2) + 1  # 4 sur 6

    @discord.ui.button(label="✅ Accepter", style=discord.ButtonStyle.green, custom_id="veto_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.players:
            return await interaction.response.send_message("Tu n'es pas dans ce match.", ephemeral=True)
        if self.resolved:
            return await interaction.response.send_message("Déjà résolu.", ephemeral=True)
        self.accepts.add(interaction.user.id)
        self.vetos.discard(interaction.user.id)
        await interaction.response.defer()
        await self._check(interaction.channel)

    @discord.ui.button(label="❌ Veto", style=discord.ButtonStyle.red, custom_id="veto_reject")
    async def veto(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.players:
            return await interaction.response.send_message("Tu n'es pas dans ce match.", ephemeral=True)
        if self.resolved:
            return await interaction.response.send_message("Déjà résolu.", ephemeral=True)
        self.vetos.add(interaction.user.id)
        self.accepts.discard(interaction.user.id)
        await interaction.response.send_message(
            f"🚫 {interaction.user.mention} veto ! ({len(self.vetos)}/{self._majority()} pour re-roll)",
            ephemeral=False
        )
        await self._check(interaction.channel)

    async def _check(self, channel):
        maj = self._majority()
        if len(self.vetos) >= maj and not self.resolved:
            self.resolved = True
            self.stop()
            await channel.send("⚡ **Veto majoritaire !** Nouveau tirage des capitaines...")
            await launch_veto(channel, self.match_id, self.players)
        elif len(self.accepts) >= maj and not self.resolved:
            self.resolved = True
            self.stop()
            await start_draft(channel, self.match_id, self.cap1, self.cap2)

    async def on_timeout(self):
        if not self.resolved:
            self.resolved = True
            match = active_matches.get(self.match_id)
            if match:
                ch = bot.get_guild(GUILD_ID).get_channel(match["draft_channel"])
                if ch:
                    await ch.send("⏰ Temps écoulé — caps validés automatiquement.")
                    await start_draft(ch, self.match_id, self.cap1, self.cap2)

# ══════════════════════════════════════════════════════
#  🎯  VIEW : DRAFT — Phase 1 : Cap1 pick 1 mate
# ══════════════════════════════════════════════════════
class DraftViewCap1(discord.ui.View):
    def __init__(self, match_id: int, cap1: int, cap2: int, pool: list[int]):
        super().__init__(timeout=120)
        self.match_id = match_id
        self.cap1     = cap1
        self.cap2     = cap2
        self.pool     = pool
        self.done     = False
        for uid in pool:
            self.add_item(PickButtonCap1(uid=uid, parent=self))

class PickButtonCap1(discord.ui.Button):
    def __init__(self, uid: int, parent: "DraftViewCap1"):
        guild = bot.get_guild(GUILD_ID)
        m     = guild.get_member(uid) if guild else None
        label = m.display_name[:25] if m else str(uid)
        super().__init__(label=label, style=discord.ButtonStyle.red, custom_id=f"pick1_{uid}")
        self.uid    = uid
        self.parent = parent

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent.cap1:
            return await interaction.response.send_message("Seul le Cap 1 peut picker ici.", ephemeral=True)
        if self.parent.done:
            return

        self.parent.done = True
        self.parent.stop()

        match = active_matches[self.parent.match_id]
        match["cap1_pick"] = self.uid

        guild       = bot.get_guild(GUILD_ID)
        picked_name = guild.get_member(self.uid).display_name

        await interaction.response.edit_message(
            content=f"🔴 **Cap 1** a pické : **{picked_name}** ✅",
            view=None
        )

        # Pool restant pour Cap2 (sans le pick de cap1)
        pool2 = [u for u in self.parent.pool if u != self.uid]
        await launch_draft_cap2(interaction.channel, self.parent.match_id, pool2)

# ══════════════════════════════════════════════════════
#  🎯  VIEW : DRAFT — Phase 2 : Cap2 pick 2 mates
# ══════════════════════════════════════════════════════
class DraftViewCap2(discord.ui.View):
    def __init__(self, match_id: int, cap2: int, pool: list[int]):
        super().__init__(timeout=120)
        self.match_id = match_id
        self.cap2     = cap2
        self.pool     = pool
        self.picked   = []
        self.done     = False
        for uid in pool:
            self.add_item(PickButtonCap2(uid=uid, parent=self))

class PickButtonCap2(discord.ui.Button):
    def __init__(self, uid: int, parent: "DraftViewCap2"):
        guild = bot.get_guild(GUILD_ID)
        m     = guild.get_member(uid) if guild else None
        label = m.display_name[:25] if m else str(uid)
        super().__init__(label=label, style=discord.ButtonStyle.blurple, custom_id=f"pick2_{uid}")
        self.uid    = uid
        self.parent = parent

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent.cap2:
            return await interaction.response.send_message("Seul le Cap 2 peut picker ici.", ephemeral=True)
        if self.uid in self.parent.picked:
            return await interaction.response.send_message("Déjà pické.", ephemeral=True)
        if len(self.parent.picked) >= 2:
            return await interaction.response.send_message("Tu as déjà tes 2 mates.", ephemeral=True)
        if self.parent.done:
            return

        self.parent.picked.append(self.uid)
        self.disabled = True
        self.style    = discord.ButtonStyle.gray

        guild        = bot.get_guild(GUILD_ID)
        picked_names = " | ".join(guild.get_member(u).display_name for u in self.parent.picked)

        await interaction.response.edit_message(
            content=f"🔵 **Cap 2** — Picks ({len(self.parent.picked)}/2) : **{picked_names}**",
            view=self.parent
        )

        if len(self.parent.picked) == 2:
            self.parent.done = True
            self.parent.stop()

            match     = active_matches[self.parent.match_id]
            cap1      = match["cap1"]
            cap1_pick = match["cap1_pick"]

            # Le dernier joueur restant va automatiquement dans team1
            last      = [u for u in self.parent.pool if u not in self.parent.picked][0]
            last_name = guild.get_member(last).display_name

            match["team2"] = [self.parent.cap2] + self.parent.picked
            match["team1"] = [cap1, cap1_pick, last]

            await interaction.channel.send(
                f"✅ **Cap 1** récupère le dernier joueur : **{last_name}**"
            )
            await finalize_draft(interaction.channel, self.parent.match_id)

# ══════════════════════════════════════════════════════
#  📊  VIEW : VOTE RÉSULTAT
# ══════════════════════════════════════════════════════
class ResultView(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=VOTE_TIMEOUT)
        self.match_id = match_id
        self.votes    = {}   # uid -> "team1" | "team2"
        self.done     = False

    @discord.ui.button(label="🔴 Team 1 gagne", style=discord.ButtonStyle.red)
    async def vote_t1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, "team1")

    @discord.ui.button(label="🔵 Team 2 gagne", style=discord.ButtonStyle.blurple)
    async def vote_t2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, "team2")

    async def _vote(self, interaction: discord.Interaction, team: str):
        match = active_matches.get(self.match_id)
        if not match or self.done:
            return await interaction.response.send_message("Vote déjà terminé.", ephemeral=True)
        all_p = match["team1"] + match["team2"]
        if interaction.user.id not in all_p:
            return await interaction.response.send_message("Tu n'es pas dans ce match.", ephemeral=True)

        self.votes[interaction.user.id] = team
        t1 = sum(1 for v in self.votes.values() if v == "team1")
        t2 = sum(1 for v in self.votes.values() if v == "team2")
        await interaction.response.send_message(
            f"✅ Vote enregistré — 🔴 {t1} | 🔵 {t2}  (4 identiques pour valider)",
            ephemeral=True
        )
        if t1 >= 4 or t2 >= 4:
            self.done = True
            self.stop()
            winner = "team1" if t1 >= 4 else "team2"
            await process_result(interaction.channel, self.match_id, winner)

    async def on_timeout(self):
        if not self.done:
            match = active_matches.get(self.match_id)
            if match:
                guild = bot.get_guild(GUILD_ID)
                data  = load_data()
                for uid in match["team1"] + match["team2"]:
                    get_player(data, uid)["pending_vote"] = None
                save_data(data)
                ch = guild.get_channel(match["draft_channel"])
                if ch:
                    await ch.send("⏰ Timeout vote résultat — match annulé, aucun Elo modifié.")
            await asyncio.sleep(CLEANUP_DELAY)
            await cleanup_match(self.match_id)

# ══════════════════════════════════════════════════════
#  🔧  LOGIQUE MATCH
# ══════════════════════════════════════════════════════
async def launch_match(players: list[int]):
    guild    = bot.get_guild(GUILD_ID)
    lobby_vc = guild.get_channel(LOBBY_VC_ID)

    # ── PHASE 1 : TP tout le monde dans le lobby intermédiaire ──
    for uid in players:
        m = guild.get_member(uid)
        if m and m.voice:
            try:
                await m.move_to(lobby_vc)
            except:
                pass

    # Attendre LOBBY_WAIT secondes — si quelqu'un leave → annulation
    await asyncio.sleep(LOBBY_WAIT)

    # Vérifier qui est encore dans le lobby
    lobby_vc  = guild.get_channel(LOBBY_VC_ID)  # refresh
    in_lobby  = [m.id for m in lobby_vc.members] if lobby_vc else []
    leaved    = [uid for uid in players if uid not in in_lobby]

    if leaved:
        leaved_names = [guild.get_member(u).display_name if guild.get_member(u) else str(u) for u in leaved]
        stayed       = [uid for uid in players if uid not in leaved]

        for uid in leaved:
            m = guild.get_member(uid)
            if m:
                try:
                    await m.send("❌ Tu as quitté le lobby — le match a été annulé.")
                except:
                    pass

        queue_vc = guild.get_channel(QUEUE_VC_ID)
        for uid in stayed:
            m = guild.get_member(uid)
            if m and m.voice:
                try:
                    await m.move_to(queue_vc)
                    if uid not in queue:
                        queue.insert(0, uid)
                except:
                    pass

        print(f"[LOBBY] Match annulé — leaver(s) : {leaved_names}")
        return

    # ── PHASE 2 : Tout le monde présent → lancer le vrai match ──
    data = load_data()
    data["match_counter"] += 1
    match_id = data["match_counter"]

    for uid in players:
        get_player(data, uid)["pending_vote"] = match_id
    save_data(data)

    category   = guild.get_channel(CATEGORY_ID)
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    for uid in players:
        m = guild.get_member(uid)
        if m:
            overwrites[m] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    draft_ch = await guild.create_text_channel(
        name=f"match-{match_id}-draft",
        category=category,
        overwrites=overwrites
    )

    active_matches[match_id] = {
        "players":       players,
        "draft_channel": draft_ch.id,
        "team1_vc":      None,
        "team2_vc":      None,
        "team1":         [],
        "team2":         [],
        "cap1":          None,
        "cap2":          None,
        "cap1_pick":     None,
        "phase":         "draft",
    }

    mentions = " ".join(guild.get_member(u).mention for u in players if guild.get_member(u))
    await draft_ch.send(
        f"⚔️ **Match #{match_id} — UHC Rivals 3v3**\n"
        f"{mentions}\n\n"
        f"✅ Tout le monde est présent — tirage des capitaines..."
    )
    await launch_veto(draft_ch, match_id, players)


async def launch_veto(channel, match_id: int, players: list[int]):
    cap1, cap2 = random.sample(players, 2)
    active_matches[match_id]["cap1"] = cap1
    active_matches[match_id]["cap2"] = cap2

    guild = bot.get_guild(GUILD_ID)
    m1    = guild.get_member(cap1)
    m2    = guild.get_member(cap2)

    view = VetoView(match_id, players, cap1, cap2)
    await channel.send(
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎲 **Capitaines proposés :**\n"
        f"🔴 Cap 1 → {m1.mention}\n"
        f"🔵 Cap 2 → {m2.mention}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"**Accepter** ou **Veto** (4/6 pour re-roll) — {VETO_TIMEOUT}s",
        view=view
    )


async def start_draft(channel, match_id: int, cap1: int, cap2: int):
    match              = active_matches[match_id]
    match["cap1"]      = cap1
    match["cap2"]      = cap2
    match["cap1_pick"] = None

    guild = bot.get_guild(GUILD_ID)
    m1    = guild.get_member(cap1)
    m2    = guild.get_member(cap2)

    # Pool = les 4 joueurs restants (ni cap1, ni cap2)
    pool       = [u for u in match["players"] if u not in (cap1, cap2)]
    pool_names = " | ".join(guild.get_member(u).display_name for u in pool)

    await channel.send(
        f"✅ **Capitaines validés !**\n"
        f"🔴 **Cap 1** : {m1.mention} — pick **1 joueur** en premier\n"
        f"🔵 **Cap 2** : {m2.mention} — pick **2 joueurs** ensuite\n"
        f"🔴 **Cap 1** : récupère le dernier joueur automatiquement\n\n"
        f"📋 Disponibles : {pool_names}"
    )

    view = DraftViewCap1(match_id=match_id, cap1=cap1, cap2=cap2, pool=pool)
    await channel.send(
        f"🔴 **Cap 1** {m1.mention} — choisis ton **1er mate** ↓",
        view=view
    )


async def launch_draft_cap2(channel, match_id: int, pool: list[int]):
    match = active_matches[match_id]
    cap2  = match["cap2"]
    guild = bot.get_guild(GUILD_ID)
    m2    = guild.get_member(cap2)

    pool_names = " | ".join(guild.get_member(u).display_name for u in pool)
    view = DraftViewCap2(match_id=match_id, cap2=cap2, pool=pool)
    await channel.send(
        f"🔵 **Cap 2** {m2.mention} — choisis tes **2 mates** ↓\n"
        f"📋 Disponibles : {pool_names}",
        view=view
    )


async def finalize_draft(channel, match_id: int):
    match    = active_matches[match_id]
    guild    = bot.get_guild(GUILD_ID)
    category = guild.get_channel(CATEGORY_ID)
    team1    = match["team1"]
    team2    = match["team2"]

    def ow(uids):
        o = {guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False)}
        for uid in uids:
            m = guild.get_member(uid)
            if m:
                o[m] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
        return o

    vc1 = await guild.create_voice_channel(f"🔴 Team 1 — Match {match_id}", category=category, overwrites=ow(team1))
    vc2 = await guild.create_voice_channel(f"🔵 Team 2 — Match {match_id}", category=category, overwrites=ow(team2))
    match["team1_vc"] = vc1.id
    match["team2_vc"] = vc2.id

    for uid in team1:
        m = guild.get_member(uid)
        if m and m.voice:
            try: await m.move_to(vc1)
            except: pass
    for uid in team2:
        m = guild.get_member(uid)
        if m and m.voice:
            try: await m.move_to(vc2)
            except: pass

    t1 = " | ".join(guild.get_member(u).display_name for u in team1)
    t2 = " | ".join(guild.get_member(u).display_name for u in team2)

    result_view = ResultView(match_id)
    await channel.send(
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚔️ **Draft terminée — Bonne chance !**\n\n"
        f"🔴 **Team 1** : {t1}\n"
        f"🔵 **Team 2** : {t2}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Votez le résultat. **4 votes identiques** valident.\n"
        f"⏰ Timeout {VOTE_TIMEOUT // 60} min → annulation sans Elo.",
        view=result_view
    )


async def process_result(channel, match_id: int, winner_team: str):
    match   = active_matches[match_id]
    guild   = bot.get_guild(GUILD_ID)
    data    = load_data()

    winners = match[winner_team]
    losers  = match["team2" if winner_team == "team1" else "team1"]

    avg_w = sum(get_player(data, u)["elo"] for u in winners) / len(winners)
    avg_l = sum(get_player(data, u)["elo"] for u in losers)  / len(losers)
    delta_w, delta_l = elo_calc(avg_w, avg_l)

    changes = []
    for uid in winners:
        p    = get_player(data, uid)
        old  = p["elo"]
        p["elo"] = max(0, p["elo"] + delta_w)
        p["wins"] += 1
        p["pending_vote"] = None
        changes.append((uid, old, p["elo"], True))

    for uid in losers:
        p    = get_player(data, uid)
        old  = p["elo"]
        p["elo"] = max(0, p["elo"] + delta_l)
        p["losses"] += 1
        p["pending_vote"] = None
        changes.append((uid, old, p["elo"], False))

    save_data(data)

    for uid, old_elo, new_elo, _ in changes:
        await update_grade_role(guild, uid, old_elo, new_elo)

    winner_name = "🔴 Team 1" if winner_team == "team1" else "🔵 Team 2"
    lines = [f"🏆 **{winner_name} a gagné le Match #{match_id} !**\n"]
    for uid, old_elo, new_elo, won in changes:
        m     = guild.get_member(uid)
        name  = m.display_name if m else str(uid)
        diff  = new_elo - old_elo
        sign  = "+" if diff >= 0 else ""
        grade, _, _ = get_grade(new_elo)
        emoji = "✅" if won else "❌"
        lines.append(f"{emoji} **{name}** : {old_elo} → **{new_elo}** Elo ({sign}{diff}) | {grade}")

    await channel.send("\n".join(lines))

    await update_leaderboard(guild, data)

    await asyncio.sleep(CLEANUP_DELAY)
    await cleanup_match(match_id)


async def cleanup_match(match_id: int):
    match = active_matches.pop(match_id, None)
    if not match:
        return
    guild = bot.get_guild(GUILD_ID)
    for ch_id in [match.get("draft_channel"), match.get("team1_vc"), match.get("team2_vc")]:
        if ch_id:
            ch = guild.get_channel(ch_id)
            if ch:
                try: await ch.delete()
                except: pass

# ══════════════════════════════════════════════════════
#  🏅  GRADES & RÔLES
# ══════════════════════════════════════════════════════
async def update_grade_role(guild: discord.Guild, uid: int, old_elo: int, new_elo: int):
    _, _, old_role_name = get_grade(old_elo)
    _, new_color, new_role_name = get_grade(new_elo)
    if old_role_name == new_role_name:
        return
    member = guild.get_member(uid)
    if not member:
        return
    old_role = discord.utils.get(guild.roles, name=old_role_name)
    if old_role and old_role in member.roles:
        try: await member.remove_roles(old_role)
        except: pass
    new_role = discord.utils.get(guild.roles, name=new_role_name)
    if not new_role:
        new_role = await guild.create_role(name=new_role_name, color=new_color)
    try: await member.add_roles(new_role)
    except: pass

# ══════════════════════════════════════════════════════
#  🏆  LEADERBOARD
# ══════════════════════════════════════════════════════
async def update_leaderboard(guild: discord.Guild, data: dict):
    ch = guild.get_channel(LEADERBOARD_CH_ID)
    if not ch:
        return

    ranked = sorted(data["players"].items(), key=lambda x: x[1]["elo"], reverse=True)
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}

    lines = [
        "```",
        "🏆  UHC RIVALS — LEADERBOARD",
        "─────────────────────────────────────────",
        f"{'#':<4} {'Joueur':<20} {'Elo':>6}  {'Grade':<14} {'W':>4} {'L':>4}",
        "─────────────────────────────────────────",
    ]
    for i, (uid_str, p) in enumerate(ranked[:20]):
        uid    = int(uid_str)
        m      = guild.get_member(uid)
        name   = (m.display_name if m else f"<@{uid}>")[:18]
        grade, _, _ = get_grade(p["elo"])
        prefix = medals.get(i, f"#{i+1}")
        lines.append(f"{prefix:<4} {name:<20} {p['elo']:>6}  {grade:<14} {p['wins']:>4} {p['losses']:>4}")

    lines += ["─────────────────────────────────────────", "```"]
    content = "\n".join(lines)

    async for msg in ch.history(limit=15):
        if msg.author == guild.me:
            try:
                await msg.edit(content=content)
                return
            except:
                break
    await ch.send(content)

# ══════════════════════════════════════════════════════
#  🎧  EVENTS
# ══════════════════════════════════════════════════════
@bot.event
async def on_ready():
    print(f"✅ {bot.user} connecté")
    guild = bot.get_guild(GUILD_ID)
    if guild:
        for _, label, color, role_name in GRADES:
            if not discord.utils.get(guild.roles, name=role_name):
                await guild.create_role(name=role_name, color=color)
                print(f"  → Rôle créé : {role_name}")
    print("Prêt ✅")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.guild.id != GUILD_ID:
        return
    guild = bot.get_guild(GUILD_ID)

    # ── Rejoint la queue ──
    if after.channel and after.channel.id == QUEUE_VC_ID:
        data   = load_data()
        player = get_player(data, member.id)

        if player["pending_vote"] is not None:
            mid   = player["pending_vote"]
            match = active_matches.get(mid)
            await member.move_to(None)
            if match:
                ch = guild.get_channel(match["draft_channel"])
                if ch:
                    try:
                        await member.send(
                            f"❌ Tu dois voter le résultat du **Match #{mid}** avant de rejoindre la queue !\n"
                            f"👉 {ch.mention}"
                        )
                    except:
                        pass
            return

        if member.id not in queue:
            queue.append(member.id)
            print(f"[QUEUE] +{member.display_name}  ({len(queue)}/{QUEUE_SIZE})")

        if len(queue) >= QUEUE_SIZE:
            players = queue[:QUEUE_SIZE]
            del queue[:QUEUE_SIZE]

            lobby_vc = guild.get_channel(LOBBY_VC_ID)
            for uid in players:
                m = guild.get_member(uid)
                if m and m.voice:
                    try: await m.move_to(lobby_vc)
                    except: pass

            print(f"[QUEUE] 6 joueurs → TP lobby, attente {LOBBY_WAIT}s")
            asyncio.create_task(launch_match(players))

    # ── Quitte la queue ──
    if before.channel and before.channel.id == QUEUE_VC_ID:
        if member.id in queue:
            queue.remove(member.id)
            print(f"[QUEUE] -{member.display_name}  ({len(queue)}/{QUEUE_SIZE})")

# ══════════════════════════════════════════════════════
#  💬  COMMANDES ADMIN & JOUEURS
# ══════════════════════════════════════════════════════
@bot.command(name="leaderboard", aliases=["lb"])
async def cmd_lb(ctx):
    data = load_data()
    await update_leaderboard(ctx.guild, data)
    await ctx.message.delete()

@bot.command(name="stats")
async def cmd_stats(ctx, member: discord.Member = None):
    target = member or ctx.author
    data   = load_data()
    p      = get_player(data, target.id)
    grade, color, _ = get_grade(p["elo"])
    total  = p["wins"] + p["losses"]
    wr     = round(p["wins"] / total * 100) if total > 0 else 0
    embed  = discord.Embed(title=f"📊 {target.display_name}", color=color)
    embed.add_field(name="Elo",   value=f"**{p['elo']}**",                        inline=True)
    embed.add_field(name="Grade", value=grade,                                     inline=True)
    embed.add_field(name="W/L",   value=f"{p['wins']}W / {p['losses']}L ({wr}%)", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="queue")
async def cmd_queue(ctx):
    guild = bot.get_guild(GUILD_ID)
    if not queue:
        return await ctx.send(f"Queue vide — {QUEUE_SIZE} joueurs nécessaires.")
    names = [guild.get_member(u).display_name for u in queue if guild.get_member(u)]
    await ctx.send(f"**Queue ({len(queue)}/{QUEUE_SIZE}) :** {', '.join(names)}")

@bot.command(name="setelo")
@commands.has_permissions(administrator=True)
async def cmd_setelo(ctx, member: discord.Member, elo: int):
    data = load_data()
    p    = get_player(data, member.id)
    old  = p["elo"]
    p["elo"] = max(0, elo)
    save_data(data)
    await update_grade_role(ctx.guild, member.id, old, p["elo"])
    await ctx.send(f"✅ Elo de **{member.display_name}** : {old} → **{p['elo']}**")

@bot.command(name="resetelo")
@commands.has_permissions(administrator=True)
async def cmd_resetelo(ctx, member: discord.Member):
    data = load_data()
    p    = get_player(data, member.id)
    old  = p["elo"]
    p["elo"] = ELO_DEFAULT
    save_data(data)
    await update_grade_role(ctx.guild, member.id, old, ELO_DEFAULT)
    await ctx.send(f"✅ Elo de **{member.display_name}** remis à {ELO_DEFAULT}.")

@bot.command(name="forcestart")
@commands.has_permissions(administrator=True)
async def cmd_forcestart(ctx):
    if len(queue) < QUEUE_SIZE:
        return await ctx.send(f"❌ {len(queue)}/{QUEUE_SIZE} joueurs dans la queue.")
    players = queue[:QUEUE_SIZE]
    del queue[:QUEUE_SIZE]
    await launch_match(players)
    await ctx.send("✅ Match forcé !")

@bot.command(name="clearqueue")
@commands.has_permissions(administrator=True)
async def cmd_clearqueue(ctx):
    queue.clear()
    await ctx.send("✅ Queue vidée.")

# ══════════════════════════════════════════════════════
#  🚀  LANCEMENT
# ══════════════════════════════════════════════════════
bot.run(TOKEN)
