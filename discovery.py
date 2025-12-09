async def start_background_discovery(hass: HomeAssistant):
    # ждём пока HA полностью встанет (по желанию)
    await hass.async_block_till_done()

    loop = hass.loop
    found_guids: set[str] = set()

    class BgProto(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr):
            host, _port = addr
            m = UUID_RE.search(data)
            if not m:
                return
            guid = m.group(0).decode("ascii")
            # если уже есть entry с таким guid — игнор
            if guid in found_guids or _guid_known(hass, guid):
                return
            found_guids.add(guid)
            # запускаем discovery-flow
            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": "discovery"},
                    data={"host": host, "guid": guid},
                )
            )

    # открываем сокеты на 54377/54378 и не закрываем, пока работает HA
    for port in (54377, 54378):
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: BgProto(),
                local_addr=("0.0.0.0", port),
            )
            # можно сохранить transport в hass.data, если захочешь потом закрывать
        except OSError:
            _LOGGER.debug("Tylo Sauna bg discovery: cannot bind %s", port)
