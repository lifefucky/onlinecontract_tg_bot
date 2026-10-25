from aiogram.utils.markdown import hbold, hlink


def beautiful_procedure(row_data):
    card = (
        f'{hlink(title="{}[{}]".format(row_data.get("owner"), row_data.get("id")), url=row_data.get("link"))}\n'
        f'{hbold("Наименование: ")}{row_data.get("name")}\n'
        f'{hbold("НДС: ")}{"{} %".format(row_data.get("nds")) if row_data.get("useNDS") else "Нет"}\n'
        f'{hbold("Начало: ")}{row_data.get("reBiddingStart")}\n'
        f'{hbold("Окончание: ")}{row_data.get("reBiddingEnd")}\n🔥'
    )
    return card


def beautiful_positions(row_data):
    cm = row_data.get("common_message") or {}
    id_val = cm.get("Id", "?")
    id_msg = f'[{hbold("КЛ-" + str(id_val))}]\n'

    common_msg = ""
    if cm:
        common = {k: v for k, v in cm.items() if v and k != "Id"}
        if common.get("ownerSrok"):
            common_msg += f'{hbold("Условия оплаты: ")}{common.get("ownerSrok")}\n'
        if common.get("ownerSklad"):
            common_msg += f'{hbold("Сроки поставки: ")}{common.get("ownerSklad")}\n'
        if common.get("deliveryPlace"):
            common_msg += f'{hbold("Место поставки: ")}{common.get("deliveryPlace")}\n'

    position_msg = ""
    for pos in row_data.get("positions") or []:
        position_msg += (
            f'{hbold("Наименование: ")}{pos.get("name")}\n'
            f'{hbold("Количество: ")}{pos.get("totalCount")} {pos.get("unit")}\n'
            f'{hbold("Цена: ")}{pos.get("price")}\n\n'
        )

    if not position_msg.strip():
        position_msg = "Позиции не указаны.\n"

    return f"{id_msg}\nУсловия:\n{common_msg}\nПозиции:\n{position_msg}"


def split_telegram_messages(text: str, max_len: int = 4096) -> list[str]:
    """Split plain or HTML text into chunks not exceeding Telegram message limit."""
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= max_len:
            chunks.append(rest)
            break
        split_at = rest.rfind("\n", 0, max_len)
        if split_at == -1:
            chunks.append(rest[:max_len])
            rest = rest[max_len:]
        else:
            # split_at — индекс «своего» \n; включаем его в чанк, иначе он теряется
            chunks.append(rest[: split_at + 1])
            rest = rest[split_at + 1 :]
    return chunks
