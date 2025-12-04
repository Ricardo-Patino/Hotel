def process_user_message(texto:str, session_id:str, codigo_cliente:int|None):
    conv = get_or_create_conversation(session_id, codigo_cliente)
    save_msg(conv.Id, "user", texto)

    intent, slots, conf = classify_and_extract(texto)
    if conf < 0.5:
        # Fallback: RAG ligero o triaje a humano
        reply = kb_search_or_handoff(texto, conv)
        save_msg(conv.Id, "bot", reply)
        return reply

    reply, action_meta = handle_intent(intent, slots, conv, codigo_cliente)
    save_msg(conv.Id, "bot", reply)

    if action_meta.get("notify"):
        NotificationService().route_and_queue(
            cliente_id=codigo_cliente,
            subject=action_meta["subject"],
            body=action_meta["body"],
            sms=action_meta.get("sms"),
            ref_entidad=action_meta.get("ref_entidad","SAC"),
            ref_id=action_meta.get("ref_id","-"),
        )
    return reply
