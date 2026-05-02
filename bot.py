import discord
from discord.ext import commands
import json, os, asyncio, random

# ══════════════════════════════════════════════════════
# ⚙️ CONFIG
# ══════════════════════════════════════════════════════
GUILD_ID = 1162542486779072664
CATEGORY_ID = 1500100508453441736
QUEUE_VC_ID = 1500100693028110406
LOBBY_VC_ID = 1500112332163121262
LEADERBOARD_CH_ID = 1500112197827825694
TOKEN = os.getenv("TOKEN", "")

QUEUE_SIZE = 6                # Taille auto-start (3v3)
LOBBY_WAIT = 15
ELO_DEFAULT = 1000
ELO_K = 32
VOTE_TIMEOUT = 600
CLEANUP_DELAY = 15
VETO_TIMEOUT = 60
DRAFT_TIMEOUT = 120
MAX_VETO_ROUNDS = 3           # NEW : anti-loop infini

DATA_FILE = "data.json"

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
# 💾 PERSISTANCE
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
            "elo": ELO_DEFAULT, "wins": 0, "losses": 0, "pending_vote": None
        }
    return data["players"][k]

def elo_calc(winner_elo: float, loser_elo: float):
    exp_w = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    delta_w = round(ELO_K * (1 - exp_w))
    delta_l = round(ELO_K * (0 - (1 - exp_w)))
    return delta_w, delta_l

# ══════════════════════════════════════════════════════
# 🤖 BOT INIT
# ══════════════════════════════════════════════════════
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

queue: list[int] = []
active_matches: dict[int, dict] = {}

# ══════════════════════════════════════════════════════
# 🛠️ HELPERS — anti-crash
# ══════════════════════════════════════════════════════
async def safe_move(member: discord.Member, channel):
    """Move sécurisé : ne crash pas si le membre n'est plus en vocal."""
    try:
        if member and member.voice:
            await member.move_to(channel)
            return True
    except (discord.HTTPException, AttributeError) as e:
        print(f"[MOVE] Échec {member.display_name if member else '?'} : {e}")
    return False

async def safe_dm(member: discord.Member, content: str):
    """DM sécurisé : ne crash pas si DMs fermés."""
    try:
        await member.send(content)
        return True
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return False

# ══════════════════════════════════════════════════════
# 🗳️ VIEW : VETO CAPITAINES
# ══════════════════════════════════════════════════════
class VetoView(discord.ui.View):
    def __init__(self, match_id: int, players: list[int], cap1: int, cap2: int, veto_round: int = 0):
        super().__init__(timeout=VETO_TIMEOUT)
        self.match_id = match_id
        self.players = players
        self.cap1 = cap1
        self.cap2 = cap2
        self.veto_round = veto_round
        self.vetos = set()
        self.accepts = set()
        self.resolved = False

    def _majority(self):
        return (len(self.players) // 2) + 1

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
            # FIX : limite anti-loop
            if self.veto_round + 1 >= MAX_VETO_ROUNDS:
                await channel.send(f"⚡ **Veto majoritaire** mais limite atteinte ({MAX_VETO_ROUNDS} re-rolls) — caps validés d'office.")
                await start_draft(channel, self.match_id, self.cap1, self.cap2)
            else:
                await channel.send("⚡ **Veto majoritaire !** Nouveau tirage des capitaines...")
                # FIX : exclure les caps qu'on vient de vetoer
                await launch_veto(channel, self.match_id, self.players,
                                  exclude=[self.cap1, self.cap2],
                                  veto_round=self.veto_round + 1)
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
# 🎯 VIEW : DRAFT — boutons "case noire" numérotés
# ══════════════════════════════════════════════════════
class DraftView(discord.ui.View):
    def __init__(self, match_id: int, cap2: int, pool: list[int], picks_needed: int):
        super().__init__(timeout=DRAFT_TIMEOUT)
        self.match_id = match_id
        self.cap2 = cap2
        self.pool = pool
        self.picks_needed = picks_needed
        self.picked = []
        self.done = False
        for i, uid in enumerate(pool):
            self.add_item(PickButton(uid=uid, parent=self, index=i + 1))

    async def on_timeout(self):
        """FIX : Cap2 AFK → picks aléatoires, le match continue."""
        if self.done:
            return
        self.done = True
        match = active_matches.get(self.match_id)
        if not match:
            return
        remaining = [u for u in self.pool if u not in self.picked]
        needed = self.picks_needed - len(self.picked)
        if needed > 0 and remaining:
            self.picked.extend(random.sample(remaining, min(needed, len(remaining))))
        match["team2"] = [self.cap2] + self.picked
        match["team1"] = [u for u in match["players"] if u not in match["team2"]]
        guild = bot.get_guild(GUILD_ID)
        channel = guild.get_channel(match["draft_channel"]) if guild else None
        if channel:
            await channel.send("⏰ **Cap 2 AFK** — picks complétés aléatoirement.")
            await finalize_draft(channel, self.match_id)


class PickButton(discord.ui.Button):
    """Bouton 'case noire' avec pseudo Discord — style secondary = gris foncé."""
    def __init__(self, uid: int, parent: DraftView, index: int):
        guild = bot.get_guild(GUILD_ID)
        m = guild.get_member(uid) if guild else None
        name = m.display_name[:20] if m else f"User{uid}"
        super().__init__(
            label=f"{index}. {name}",
            style=discord.ButtonStyle.secondary,  # gris foncé = "case noire"
            custom_id=f"pick_{uid}"
        )
        self.uid = uid
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        p = self.parent_view
        if interaction.user.id != p.cap2:
            return await interaction.response.send_message("Seul le **Cap 2** peut picker.", ephemeral=True)
        if p.done:
            return await interaction.response.send_message("Draft déjà terminée.", ephemeral=True)
        if self.uid in p.picked:
            return await interaction.response.send_message("Déjà pické.", ephemeral=True)
        if len(p.picked) >= p.picks_needed:
            return await interaction.response.send_message("Tu as déjà tous tes mates.", ephemeral=True)

        p.picked.append(self.uid)
        self.disabled = True
        self.style = discord.ButtonStyle.success  # vert = pické

        guild = bot.get_guild(GUILD_ID)
        picked_names = " | ".join(
            (guild.get_member(u).display_name if guild.get_member(u) else f"User{u}")
            for u in p.picked
        )
        await interaction.response.edit_message(
            content=f"🔵 **Cap 2** — Picks ({len(p.picked)}/{p.picks_needed}) : **{picked_names}**",
            view=p
        )

        if len(p.picked) >= p.picks_needed:
            p.done = True
            p.stop()
            match = active_matches[p.match_id]
            match["team2"] = [p.cap2] + p.picked
            match["team1"] = [u for u in match["players"] if u not in match["team2"]]
            await finalize_draft(interaction.channel, p.match_id)


# ══════════════════════════════════════════════════════
# 📊 VIEW : VOTE RÉSULTAT — seuil dynamique selon taille équipe
# ══════════════════════════════════════════════════════
class ResultView(discord.ui.View):
    def __init__(self, match_id: int, votes_needed: int):
        super().__init__(timeout=VOTE_TIMEOUT)
        self.match_id = match_id
        self.votes_needed = votes_needed
        self.votes = {}
        self.done = False

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
            f"✅ Vote enregistré — 🔴 {t1} | 🔵 {t2} ({self.votes_needed} identiques pour valider)",
            ephemeral=True
        )
        if t1 >= self.votes_needed or t2 >= self.votes_needed:
            self.done = True
            self.stop()
            winner = "team1" if t1 >= self.votes_needed else "team2"
            await process_result(interaction.channel, self.match_id, winner)

    async def on_timeout(self):
        if self.done:
            return
        match = active_matches.get(self.match_id)
        if not match:
            return
        guild = bot.get_guild(GUILD_ID)
        data = load_data()
        for uid in match["team1"] + match["team2"]:
            get_player(data, uid)["pending_vote"] = None
        save_data(data)
        ch = guild.get_channel(match["draft_channel"])
        if ch:
            await ch.send("⏰ Timeout vote résultat — match annulé, aucun Elo modifié.")
        await asyncio.sleep(CLEANUP_DELAY)
        await cleanup_match(self.match_id)


# ══════════════════════════════════════════════════════
# 🔧 LOGIQUE MATCH — supporte 1v1 / 2v2 / 3v3 dynamiquement
# ══════════════════════════════════════════════════════
async def launch_match(players: list[int]):
    guild = bot.get_guild(GUILD_ID)
    lobby_vc = guild.get_channel(LOBBY_VC_ID)
    team_size = len(players) // 2

    # ── PHASE 1 : TP lobby ──
    for uid in players:
        m = guild.get_member(uid)
        if m and m.voice:
            await safe_move(m, lobby_vc)

    await asyncio.sleep(LOBBY_WAIT)

    lobby_vc = guild.get_channel(LOBBY_VC_ID)
    in_lobby = {m.id for m in lobby_vc.members} if lobby_vc else set()
    leaved = [uid for uid in players if uid not in in_lobby]

    if leaved:
        leaved_names = []
        for uid in leaved:
            m = guild.get_member(uid)
            leaved_names.append(m.display_name if m else f"User{uid}")
            if m:
                await safe_dm(m, "❌ Tu as quitté le lobby — le match a été annulé.")

        queue_vc = guild.get_channel(QUEUE_VC_ID)
        stayed = [uid for uid in players if uid not in leaved]
        for uid in stayed:
            m = guild.get_member(uid)
            if m and m.voice:
                await safe_move(m, queue_vc)
            if uid not in queue:
                queue.insert(0, uid)

        print(f"[LOBBY] Match annulé — leaver(s) : {leaved_names}")
        return

    # ── PHASE 2 : Tout présent → vrai match ──
    data = load_data()
    data["match_counter"] += 1
    match_id = data["match_counter"]

    for uid in players:
        get_player(data, uid)["pending_vote"] = match_id
    save_data(data)

    category = guild.get_channel(CATEGORY_ID)
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    for uid in players:
        m = guild.get_member(uid)
        if m:
            overwrites[m] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    draft_ch = await guild.create_text_channel(
        name=f"match-{match_id}-draft", category=category, overwrites=overwrites
    )

    active_matches[match_id] = {
        "players": players,
        "team_size": team_size,
        "draft_channel": draft_ch.id,
        "team1_vc": None, "team2_vc": None,
        "team1": [], "team2": [],
        "cap1": None, "cap2": None,
        "phase": "draft",
    }

    mentions = " ".join(guild.get_member(u).mention for u in players if guild.get_member(u))
    await draft_ch.send(
        f"⚔️ **Match #{match_id} — UHC Rivals {team_size}v{team_size}**\n"
        f"{mentions}\n\n"
        f"✅ Tout le monde est présent — tirage des capitaines..."
    )
    await launch_veto(draft_ch, match_id, players)


async def launch_veto(channel, match_id: int, players: list[int],
                      exclude: list = None, veto_round: int = 0):
    """FIX : exclude évite de retomber sur les mêmes caps après un veto."""
    pool = [p for p in players if p not in (exclude or [])]
    if len(pool) < 2:
        pool = players  # safety fallback
    cap1, cap2 = random.sample(pool, 2)

    active_matches[match_id]["cap1"] = cap1
    active_matches[match_id]["cap2"] = cap2

    guild = bot.get_guild(GUILD_ID)
    m1 = guild.get_member(cap1)
    m2 = guild.get_member(cap2)

    view = VetoView(match_id, players, cap1, cap2, veto_round=veto_round)
    round_label = f" (Re-roll {veto_round}/{MAX_VETO_ROUNDS})" if veto_round > 0 else ""
    await channel.send(
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎲 **Capitaines proposés{round_label} :**\n"
        f"🔴 Cap 1 → {m1.mention if m1 else cap1}\n"
        f"🔵 Cap 2 → {m2.mention if m2 else cap2}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"**Accepter** ou **Veto** ({(len(players)//2)+1}/{len(players)} pour re-roll) — {VETO_TIMEOUT}s",
        view=view
    )


async def start_draft(channel, match_id: int, cap1: int, cap2: int):
    match = active_matches[match_id]
    match["cap1"] = cap1
    match["cap2"] = cap2
    team_size = match["team_size"]
    picks_needed = team_size - 1  # cap2 pick (taille_équipe - 1) mates

    guild = bot.get_guild(GUILD_ID)
    m1 = guild.get_member(cap1)
    m2 = guild.get_member(cap2)

    pool = [u for u in match["players"] if u not in (cap1, cap2)]

    await channel.send(
        f"✅ **Capitaines validés !**\n"
        f"🔴 **Cap 1** : {m1.mention if m1 else cap1} — auto ✅\n"
        f"🔵 **Cap 2** : {m2.mention if m2 else cap2} — choisis tes "
        f"**{picks_needed} mate{'s' if picks_needed > 1 else ''}** ↓"
    )

    # Cas 1v1 : pas de pick
    if picks_needed == 0:
        match["team1"] = [cap1]
        match["team2"] = [cap2]
        await finalize_draft(channel, match_id)
        return

    view = DraftView(match_id=match_id, cap2=cap2, pool=pool, picks_needed=picks_needed)
    pool_names = " | ".join(
        (guild.get_member(u).display_name if guild.get_member(u) else f"User{u}") for u in pool
    )
    await channel.send(
        f"🔵 **Cap 2** — Picks (0/{picks_needed}) : —\n📋 Disponibles : {pool_names}",
        view=view
    )


async def finalize_draft(channel, match_id: int):
    match = active_matches[match_id]
    guild = bot.get_guild(GUILD_ID)
    category = guild.get_channel(CATEGORY_ID)
    team1 = match["team1"]
    team2 = match["team2"]

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
        if m: await safe_move(m, vc1)
    for uid in team2:
        m = guild.get_member(uid)
        if m: await safe_move(m, vc2)

    t1 = " | ".join((guild.get_member(u).display_name if guild.get_member(u) else f"User{u}") for u in team1)
    t2 = " | ".join((guild.get_member(u).display_name if guild.get_member(u) else f"User{u}") for u in team2)

    total_players = len(team1) + len(team2)
    votes_needed = (total_players // 2) + 1

    result_view = ResultView(match_id, votes_needed)
    match["phase"] = "result"
    await channel.send(
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚔️ **Draft terminée — Bonne chance !**\n\n"
        f"🔴 **Team 1** : {t1}\n"
        f"🔵 **Team 2** : {t2}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Votez le résultat. **{votes_needed} votes identiques** valident.\n"
        f"⏰ Timeout {VOTE_TIMEOUT // 60} min → annulation sans Elo.\n"
        f"⚖️ Admin peut forcer : `!forcewin {match_id} 1` ou `!forcewin {match_id} 2`",
        view=result_view
    )


async def process_result(channel, match_id: int, winner_team: str):
    """FIX : idempotent — pas de double-traitement si vote ET forcewin se croisent."""
    match = active_matches.get(match_id)
    if not match:
        return
    if match.get("phase") == "done":
        return
    match["phase"] = "done"

    guild = bot.get_guild(GUILD_ID)
    data = load_data()

    winners = match[winner_team]
    losers = match["team2" if winner_team == "team1" else "team1"]

    if not winners or not losers:
        await channel.send("⚠️ Impossible de traiter le résultat (équipes vides).")
        return

    avg_w = sum(get_player(data, u)["elo"] for u in winners) / len(winners)
    avg_l = sum(get_player(data, u)["elo"] for u in losers) / len(losers)
    delta_w, delta_l = elo_calc(avg_w, avg_l)

    changes = []
    for uid in winners:
        p = get_player(data, uid)
        old = p["elo"]
        p["elo"] = max(0, p["elo"] + delta_w)
        p["wins"] += 1
        p["pending_vote"] = None
        changes.append((uid, old, p["elo"], True))

    for uid in losers:
        p = get_player(data, uid)
        old = p["elo"]
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
        m = guild.get_member(uid)
        name = m.display_name if m else f"User{uid}"
        diff = new_elo - old_elo
        sign = "+" if diff >= 0 else ""
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
    if not guild:
        return
    for ch_id in [match.get("draft_channel"), match.get("team1_vc"), match.get("team2_vc")]:
        if ch_id:
            ch = guild.get_channel(ch_id)
            if ch:
                try:
                    await ch.delete()
                except discord.HTTPException as e:
                    print(f"[CLEANUP] Erreur suppression {ch_id}: {e}")


# ══════════════════════════════════════════════════════
# 🏅 GRADES
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
        except discord.HTTPException: pass
    new_role = discord.utils.get(guild.roles, name=new_role_name)
    if not new_role:
        try:
            new_role = await guild.create_role(name=new_role_name, color=new_color)
        except discord.HTTPException:
            return
    try: await member.add_roles(new_role)
    except discord.HTTPException: pass


# ══════════════════════════════════════════════════════
# 🏆 LEADERBOARD
# ══════════════════════════════════════════════════════
async def update_leaderboard(guild: discord.Guild, data: dict):
    ch = guild.get_channel(LEADERBOARD_CH_ID)
    if not ch:
        print(f"[LB] Channel introuvable : {LEADERBOARD_CH_ID}")
        return

    ranked = sorted(data["players"].items(), key=lambda x: x[1]["elo"], reverse=True)
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}

    lines = [
        "```",
        "🏆 UHC RIVALS — LEADERBOARD",
        "─────────────────────────────────────────",
        f"{'#':<4} {'Joueur':<20} {'Elo':>6} {'Grade':<14} {'W':>4} {'L':>4}",
        "─────────────────────────────────────────",
    ]
    for i, (uid_str, p) in enumerate(ranked[:20]):
        uid = int(uid_str)
        m = guild.get_member(uid)
        name = (m.display_name if m else f"User{uid}")[:18]
        grade, _, _ = get_grade(p["elo"])
        prefix = medals.get(i, f"#{i+1}")
        lines.append(f"{prefix:<4} {name:<20} {p['elo']:>6} {grade:<14} {p['wins']:>4} {p['losses']:>4}")

    lines += ["─────────────────────────────────────────", "```"]
    content = "\n".join(lines)

    try:
        async for msg in ch.history(limit=15):
            if msg.author == guild.me:
                await msg.edit(content=content)
                return
        await ch.send(content)
    except discord.HTTPException as e:
        print(f"[LB] Erreur: {e}")


# ══════════════════════════════════════════════════════
# 🎧 EVENTS
# ══════════════════════════════════════════════════════
@bot.event
async def on_ready():
    print(f"✅ {bot.user} connecté")
    guild = bot.get_guild(GUILD_ID)
    if guild:
        for _, label, color, role_name in GRADES:
            if not discord.utils.get(guild.roles, name=role_name):
                try:
                    await guild.create_role(name=role_name, color=color)
                    print(f"  → Rôle créé : {role_name}")
                except discord.HTTPException:
                    pass
    print("Prêt ✅")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.guild.id != GUILD_ID:
        return
    guild = bot.get_guild(GUILD_ID)

    before_id = before.channel.id if before.channel else None
    after_id = after.channel.id if after.channel else None

    # FIX : on filtre — l'event ne nous intéresse QUE si le channel change
    # (sinon mute/deafen/video déclenchent toute la logique queue → catastrophe)
    if before_id == after_id:
        return

    # ── Rejoint la queue ──
    if after_id == QUEUE_VC_ID:
        data = load_data()
        player = get_player(data, member.id)

        # FIX BUG CRITIQUE : nettoyer les pending_vote orphelins
        if player["pending_vote"] is not None:
            mid = player["pending_vote"]
            match = active_matches.get(mid)
            if not match:
                # Match disparu (bot crash, cancelmatch, etc.) → on nettoie
                player["pending_vote"] = None
                save_data(data)
                print(f"[QUEUE] Pending_vote orphelin nettoyé pour {member.display_name}")
            else:
                # Match toujours actif → bloquer
                await safe_move(member, None)
                ch = guild.get_channel(match["draft_channel"])
                msg = (
                    f"❌ Tu dois voter le résultat du **Match #{mid}** avant de rejoindre la queue !\n"
                    f"👉 {ch.mention if ch else f'Match #{mid}'}"
                )
                if not await safe_dm(member, msg):
                    # FIX : fallback si DMs fermés
                    if ch:
                        await ch.send(f"{member.mention} {msg}")
                return

        if member.id not in queue:
            queue.append(member.id)
            print(f"[QUEUE] +{member.display_name} ({len(queue)}/{QUEUE_SIZE})")

        if len(queue) >= QUEUE_SIZE:
            players = queue[:QUEUE_SIZE]
            del queue[:QUEUE_SIZE]

            lobby_vc = guild.get_channel(LOBBY_VC_ID)
            for uid in players:
                m = guild.get_member(uid)
                if m: await safe_move(m, lobby_vc)

            print(f"[QUEUE] {QUEUE_SIZE} joueurs → TP lobby, attente {LOBBY_WAIT}s")
            asyncio.create_task(launch_match(players))

    # ── Quitte la queue ──
    elif before_id == QUEUE_VC_ID:
        if member.id in queue:
            queue.remove(member.id)
            print(f"[QUEUE] -{member.display_name} ({len(queue)}/{QUEUE_SIZE})")


# ══════════════════════════════════════════════════════
# 💬 COMMANDES JOUEURS
# ══════════════════════════════════════════════════════
@bot.command(name="leaderboard", aliases=["lb"])
async def cmd_lb(ctx):
    data = load_data()
    await update_leaderboard(ctx.guild, data)
    try: await ctx.message.delete()
    except: pass


@bot.command(name="stats")
async def cmd_stats(ctx, member: discord.Member = None):
    target = member or ctx.author
    data = load_data()
    p = get_player(data, target.id)
    grade, color, _ = get_grade(p["elo"])
    total = p["wins"] + p["losses"]
    wr = round(p["wins"] / total * 100) if total > 0 else 0
    embed = discord.Embed(title=f"📊 {target.display_name}", color=color)
    embed.add_field(name="Elo", value=f"**{p['elo']}**", inline=True)
    embed.add_field(name="Grade", value=grade, inline=True)
    embed.add_field(name="W/L", value=f"{p['wins']}W / {p['losses']}L ({wr}%)", inline=True)
    if p.get("pending_vote"):
        embed.set_footer(text=f"⚠️ Vote en attente : Match #{p['pending_vote']}")
    await ctx.send(embed=embed)


@bot.command(name="queue")
async def cmd_queue(ctx):
    guild = bot.get_guild(GUILD_ID)
    if not queue:
        return await ctx.send(f"Queue vide — {QUEUE_SIZE} joueurs nécessaires.")
    names = [(guild.get_member(u).display_name if guild.get_member(u) else f"User{u}") for u in queue]
    await ctx.send(f"**Queue ({len(queue)}/{QUEUE_SIZE}) :** {', '.join(names)}")


@bot.command(name="matches")
async def cmd_matches(ctx):
    """Liste les matchs en cours."""
    if not active_matches:
        return await ctx.send("Aucun match en cours.")
    lines = ["**Matchs en cours :**"]
    for mid, m in active_matches.items():
        lines.append(f"• Match #{mid} — phase: `{m.get('phase', '?')}` — {len(m['players'])} joueurs ({m.get('team_size', '?')}v{m.get('team_size', '?')})")
    await ctx.send("\n".join(lines))


# ══════════════════════════════════════════════════════
# 🛡️ COMMANDES ADMIN
# ══════════════════════════════════════════════════════
@bot.command(name="setelo")
@commands.has_permissions(administrator=True)
async def cmd_setelo(ctx, member: discord.Member, elo: int):
    data = load_data()
    p = get_player(data, member.id)
    old = p["elo"]
    p["elo"] = max(0, elo)
    save_data(data)
    await update_grade_role(ctx.guild, member.id, old, p["elo"])
    await ctx.send(f"✅ Elo de **{member.display_name}** : {old} → **{p['elo']}**")


@bot.command(name="resetelo")
@commands.has_permissions(administrator=True)
async def cmd_resetelo(ctx, member: discord.Member):
    data = load_data()
    p = get_player(data, member.id)
    old = p["elo"]
    p["elo"] = ELO_DEFAULT
    save_data(data)
    await update_grade_role(ctx.guild, member.id, old, ELO_DEFAULT)
    await ctx.send(f"✅ Elo de **{member.display_name}** remis à {ELO_DEFAULT}.")


@bot.command(name="forcestart")
@commands.has_permissions(administrator=True)
async def cmd_forcestart(ctx, size: int = None):
    """NEW : force avec N joueurs (2, 4, 6...). Défaut = QUEUE_SIZE.
    Usage: !forcestart        → 6 joueurs (3v3)
           !forcestart 4      → 4 joueurs (2v2)
           !forcestart 2      → 2 joueurs (1v1)
    """
    target_size = size if size else QUEUE_SIZE
    if target_size < 2 or target_size % 2 != 0:
        return await ctx.send("❌ Taille invalide — doit être pair et ≥ 2 (ex: 2, 4, 6).")
    if len(queue) < target_size:
        return await ctx.send(f"❌ Pas assez de joueurs : {len(queue)}/{target_size} dans la queue.")
    players = queue[:target_size]
    del queue[:target_size]
    asyncio.create_task(launch_match(players))
    await ctx.send(f"✅ Match forcé en **{target_size//2}v{target_size//2}** ({target_size} joueurs) !")


@bot.command(name="forcewin")
@commands.has_permissions(administrator=True)
async def cmd_forcewin(ctx, match_id: int, team: int):
    """NEW : force la victoire d'une équipe. Usage: !forcewin <match_id> <1|2>"""
    if team not in (1, 2):
        return await ctx.send("❌ team doit être **1** ou **2**.")
    match = active_matches.get(match_id)
    if not match:
        return await ctx.send(f"❌ Match #{match_id} introuvable. Tape `!matches` pour voir les matchs en cours.")
    if not match.get("team1") or not match.get("team2"):
        return await ctx.send("❌ Draft pas encore terminée — impossible de désigner un gagnant.")
    if match.get("phase") == "done":
        return await ctx.send(f"❌ Match #{match_id} déjà terminé.")

    winner = f"team{team}"
    channel = ctx.guild.get_channel(match["draft_channel"])
    if channel:
        await channel.send(f"⚖️ **Admin {ctx.author.display_name} a forcé la victoire de Team {team}**")
        await process_result(channel, match_id, winner)
    await ctx.send(f"✅ Match #{match_id} : **Team {team}** gagne (forcé par admin).")


@bot.command(name="cancelmatch")
@commands.has_permissions(administrator=True)
async def cmd_cancelmatch(ctx, match_id: int):
    """NEW : annule un match sans toucher aux Elo."""
    match = active_matches.get(match_id)
    if not match:
        return await ctx.send(f"❌ Match #{match_id} introuvable.")
    data = load_data()
    for uid in match["players"]:
        get_player(data, uid)["pending_vote"] = None
    save_data(data)
    channel = ctx.guild.get_channel(match["draft_channel"])
    if channel:
        await channel.send(f"🛑 **Match annulé par {ctx.author.display_name}** — aucun Elo modifié.")
    await asyncio.sleep(2)
    await cleanup_match(match_id)
    await ctx.send(f"✅ Match #{match_id} annulé.")


@bot.command(name="clearvote")
@commands.has_permissions(administrator=True)
async def cmd_clearvote(ctx, member: discord.Member):
    """NEW : débloque un joueur stuck avec un pending_vote."""
    data = load_data()
    p = get_player(data, member.id)
    old = p.get("pending_vote")
    p["pending_vote"] = None
    save_data(data)
    await ctx.send(f"✅ Vote en attente nettoyé pour **{member.display_name}** (était : `{old}`).")


@bot.command(name="clearallvotes")
@commands.has_permissions(administrator=True)
async def cmd_clearallvotes(ctx):
    """NEW : urgence — débloque TOUS les joueurs stuck."""
    data = load_data()
    count = 0
    for uid_str, p in data["players"].items():
        if p.get("pending_vote") is not None:
            p["pending_vote"] = None
            count += 1
    save_data(data)
    await ctx.send(f"✅ {count} votes en attente nettoyés.")


@bot.command(name="clearqueue")
@commands.has_permissions(administrator=True)
async def cmd_clearqueue(ctx):
    queue.clear()
    await ctx.send("✅ Queue vidée.")


@bot.command(name="adduser")
@commands.has_permissions(administrator=True)
async def cmd_adduser(ctx, member: discord.Member):
    """NEW : ajoute un joueur à la queue manuellement (doit être en vocal)."""
    if member.id in queue:
        return await ctx.send(f"❌ {member.display_name} est déjà dans la queue.")
    if not member.voice:
        return await ctx.send(f"⚠️ {member.display_name} n'est pas en vocal — il sera flagué leaver au lobby.")
    queue.append(member.id)
    await ctx.send(f"✅ **{member.display_name}** ajouté à la queue ({len(queue)}/{QUEUE_SIZE}).")


# ══════════════════════════════════════════════════════
# 🚨 GESTION D'ERREUR GLOBALE
# ══════════════════════════════════════════════════════
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Tu n'as pas les permissions pour cette commande.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Argument manquant — `!help {ctx.command}` pour la syntaxe.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Argument invalide — `!help {ctx.command}` pour la syntaxe.")
    elif isinstance(error, commands.CommandNotFound):
        return
    else:
        print(f"[ERR] {ctx.command}: {error}")
        try: await ctx.send(f"❌ Erreur : `{type(error).__name__}`")
        except: pass


# ══════════════════════════════════════════════════════
# 🚀 LANCEMENT
# ══════════════════════════════════════════════════════
bot.run(TOKEN)
