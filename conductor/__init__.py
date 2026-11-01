from redbot.core.bot import Red

from .conductor import Conductor


async def setup(bot: Red) -> None:
    await bot.add_cog(Conductor(bot))
