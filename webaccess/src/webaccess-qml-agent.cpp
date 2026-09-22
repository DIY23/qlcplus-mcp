/*
  Q Light Controller Plus
  webaccess-qml-agent.cpp

  Agent API: create and manage a show project live, over the existing
  Web Access websocket. Implemented as part of WebAccessQml so it can reuse
  the document pointer, the Virtual Console helpers and the existing
  widget/function serialisation code.

  Wire format (text, pipe delimited, same socket as the rest of the web API):

      request   QLC+AGENT|<reqId>|<verb>|<base64(json-args)>
      reply     QLC+AGENT|<verb>|<reqId>|ok|<base64(json-result)>
      error     QLC+AGENT|<verb>|<reqId>|err|<base64(json {"error": "..."})>

  Payloads are base64 so that pipes or newlines inside user text (scene names,
  captions) can never break framing. One request produces exactly one reply,
  always correlated by reqId.

  Licensed under the Apache License, Version 2.0 (the "License");
  you may not use this file except in compliance with the License.
  You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0.txt

  Unless required by applicable law or agreed to in writing, software
  distributed under the License is distributed on an "AS IS" BASIS,
  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
  See the License for the specific language governing permissions and
  limitations under the License.
*/

#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <QJsonValue>
#include <QJsonParseError>
#include <QXmlStreamReader>
#include <QXmlStreamWriter>
#include <QFile>
#include <QColor>

#include "webaccess-qml.h"

#include "doc.h"
#include "fixture.h"
#include "function.h"
#include "scene.h"
#include "chaser.h"
#include "chaserstep.h"
#include "collection.h"
#include "sequence.h"
#include "inputoutputmap.h"
#include "inputpatch.h"
#include "outputpatch.h"
#include "qlcfixturedef.h"
#include "qlcfixturemode.h"
#include "qlcfixturedefcache.h"
#include "qlcchannel.h"
#include "universe.h"

#include "virtualconsole.h"
#include "vcframe.h"
#include "vcpage.h"
#include "vcwidget.h"

/* Workspace-level XML element names live in qmlui/app.h; redeclared here so the
 * agent code does not depend on the whole App class. */
#define KXMLQLCWorkspace            QStringLiteral("Workspace")
#define KXMLQLCWorkspaceWindow      QStringLiteral("CurrentWindow")

/****************************************************************************
 * Small helpers
 ****************************************************************************/

static QString agentEncode(const QJsonObject &obj)
{
    return QString::fromLatin1(QJsonDocument(obj).toJson(QJsonDocument::Compact).toBase64());
}

static QString agentOk(const QString &verb, const QString &reqId, const QJsonObject &result)
{
    return QString("QLC+AGENT|%1|%2|ok|%3").arg(verb, reqId, agentEncode(result));
}

static QString agentErr(const QString &verb, const QString &reqId, const QString &message)
{
    QJsonObject obj;
    obj["error"] = message;
    return QString("QLC+AGENT|%1|%2|err|%3").arg(verb, reqId, agentEncode(obj));
}

static QJsonObject agentDecode(const QString &base64, QString &error)
{
    error.clear();
    if (base64.isEmpty())
        return QJsonObject();

    QJsonParseError parseError;
    QJsonDocument doc = QJsonDocument::fromJson(QByteArray::fromBase64(base64.toLatin1()), &parseError);
    if (parseError.error != QJsonParseError::NoError || doc.isObject() == false)
    {
        error = QString("malformed args: %1").arg(parseError.errorString());
        return QJsonObject();
    }
    return doc.object();
}

static QString xmlEscape(const QString &text)
{
    QString out = text;
    out.replace("&", "&amp;");
    out.replace("<", "&lt;");
    out.replace(">", "&gt;");
    out.replace("\"", "&quot;");
    return out;
}

static quint32 jsonId(const QJsonObject &obj, const char *key, quint32 fallback = Function::invalidId())
{
    const QJsonValue value = obj.value(QLatin1String(key));
    if (value.isDouble())
        return quint32(value.toDouble());
    return fallback;
}

static QString jsonString(const QJsonObject &obj, const char *key, const QString &fallback = QString())
{
    const QJsonValue value = obj.value(QLatin1String(key));
    if (value.isString())
        return value.toString();
    return fallback;
}

/** JSON value -> QVariant, with colour strings turned into QColor so that
 *  VCWidget colour properties accept them. */
static QVariant agentVariant(const QString &key, const QJsonValue &value)
{
    if (key.endsWith("Color", Qt::CaseInsensitive) && value.isString())
    {
        QColor color(value.toString());
        if (color.isValid())
            return color;
    }
    return value.toVariant();
}

/** XML element name for a Virtual Console widget type name. */
static QString widgetElement(const QString &type)
{
    if (type.compare("Button", Qt::CaseInsensitive) == 0)         return "Button";
    if (type.compare("Slider", Qt::CaseInsensitive) == 0)         return "Slider";
    if (type.compare("CueList", Qt::CaseInsensitive) == 0)        return "CueList";
    if (type.compare("Label", Qt::CaseInsensitive) == 0)          return "Label";
    if (type.compare("XYPad", Qt::CaseInsensitive) == 0)          return "XYPad";
    if (type.compare("SpeedDial", Qt::CaseInsensitive) == 0)      return "SpeedDial";
    if (type.compare("Frame", Qt::CaseInsensitive) == 0)          return "Frame";
    if (type.compare("SoloFrame", Qt::CaseInsensitive) == 0)      return "SoloFrame";
    if (type.compare("Clock", Qt::CaseInsensitive) == 0)          return "Clock";
    if (type.compare("Animation", Qt::CaseInsensitive) == 0)      return "Matrix";
    if (type.compare("AudioTriggers", Qt::CaseInsensitive) == 0)  return "AudioTriggers";
    return QString();
}

/** Build the XML description of one widget, exactly as the project file stores it. */
static QByteArray widgetXml(const QString &element, quint32 id, const QString &caption,
                            int x, int y, int w, int h, quint32 functionId)
{
    QString xml = QString("<%1 ID=\"%2\" Caption=\"%3\">").arg(element).arg(id).arg(xmlEscape(caption));
    xml += QString("<WindowState Visible=\"true\" X=\"%1\" Y=\"%2\" Width=\"%3\" Height=\"%4\"/>")
               .arg(x).arg(y).arg(w).arg(h);
    if (functionId != Function::invalidId())
        xml += QString("<Function ID=\"%1\"/>").arg(functionId);
    xml += QString("</%1>").arg(element);
    return xml.toUtf8();
}

/****************************************************************************
 * Command handling
 ****************************************************************************/

QString WebAccessQml::handleAgentCommand(const QStringList &cmdList)
{
    if (cmdList.count() < 3)
        return QString();

    const QString reqId = cmdList.at(1);
    const QString verb = cmdList.at(2);
    const QString payload = cmdList.count() > 3 ? cmdList.at(3) : QString();

    QString decodeError;
    QJsonObject args = agentDecode(payload, decodeError);
    if (decodeError.isEmpty() == false)
        return agentErr(verb, reqId, decodeError);

    /************************************************************************
     * Reads
     ************************************************************************/

    if (verb == "getState")
    {
        QJsonObject result;

        QJsonObject app;
        app["name"] = QString(APPNAME);
        app["version"] = QString(APPVERSION);
        app["modified"] = m_doc->isModified();
        result["app"] = app;

        /* Universes and their patches */
        InputOutputMap *iomap = m_doc->inputOutputMap();
        QJsonArray universes;
        const QList<Universe*> universeList = iomap->universes();
        for (Universe *universe : universeList)
        {
            QJsonObject uniObj;
            uniObj["id"] = int(universe->id());

            InputPatch *inPatch = iomap->inputPatch(universe->id());
            if (inPatch != nullptr)
            {
                QJsonObject in;
                in["plugin"] = inPatch->pluginName();
                in["line"] = int(inPatch->input());
                in["name"] = inPatch->inputName();
                in["profile"] = inPatch->profileName();
                uniObj["input"] = in;
            }

            QJsonArray outputs;
            for (int p = 0; p < iomap->outputPatchesCount(universe->id()); p++)
            {
                OutputPatch *outPatch = iomap->outputPatch(universe->id(), p);
                if (outPatch == nullptr)
                    continue;
                QJsonObject out;
                out["plugin"] = outPatch->pluginName();
                out["line"] = int(outPatch->output());
                out["name"] = outPatch->outputName();
                out["patched"] = outPatch->isPatched();
                outputs.append(out);
            }
            uniObj["outputs"] = outputs;

            OutputPatch *fbPatch = iomap->feedbackPatch(universe->id());
            if (fbPatch != nullptr)
            {
                QJsonObject fb;
                fb["plugin"] = fbPatch->pluginName();
                fb["line"] = int(fbPatch->output());
                uniObj["feedback"] = fb;
            }

            universes.append(uniObj);
        }
        result["universes"] = universes;

        /* Patched fixtures */
        QJsonArray fixtures;
        const QList<Fixture*> fixtureList = m_doc->fixtures();
        for (Fixture *fixture : fixtureList)
        {
            QJsonObject fxi;
            fxi["id"] = int(fixture->id());
            fxi["name"] = fixture->name();
            fxi["type"] = fixture->typeString();
            fxi["universe"] = int(fixture->universe());
            fxi["address"] = int(fixture->address());
            fxi["channels"] = int(fixture->channels());
            QLCFixtureDef *def = fixture->fixtureDef();
            if (def != nullptr)
            {
                fxi["manufacturer"] = def->manufacturer();
                fxi["model"] = def->model();
            }
            QLCFixtureMode *mode = fixture->fixtureMode();
            if (mode != nullptr)
                fxi["mode"] = mode->name();
            fixtures.append(fxi);
        }
        result["fixtures"] = fixtures;

        /* Functions */
        QJsonArray functions;
        const QList<Function*> functionList = m_doc->functions();
        for (Function *function : functionList)
        {
            if (function->isVisible() == false)
                continue;
            QJsonObject fn;
            fn["id"] = int(function->id());
            fn["name"] = function->name();
            fn["type"] = function->typeString();
            fn["running"] = function->isRunning();
            fn["duration"] = int(function->totalDuration());
            functions.append(fn);
        }
        result["functions"] = functions;

        /* Virtual Console pages, with their widgets */
        QJsonArray pages;
        if (m_vc != nullptr)
        {
            for (int i = 0; i < m_vc->pagesCount(); i++)
            {
                VCFrame *page = m_vc->page(i);
                QJsonObject pageObj;
                pageObj["index"] = i;

                QJsonArray widgets;
                QList<VCWidget*> widgetList;
                collectWidgets(page, widgetList, true);
                for (VCWidget *widget : widgetList)
                {
                    QJsonObject w = baseWidgetToJson(widget);
                    widgets.append(w);
                }
                pageObj["widgets"] = widgets;
                pages.append(pageObj);
            }
            result["selectedPage"] = m_vc->selectedPage();
        }
        result["pages"] = pages;

        return agentOk(verb, reqId, result);
    }
    else if (verb == "listFixtureDefs")
    {
        const QString manufFilter = jsonString(args, "manufacturer");
        const QString modelFilter = jsonString(args, "model");

        QJsonArray manufacturers;
        const QStringList manufList = m_doc->fixtureDefCache()->manufacturers();
        for (const QString &manuf : manufList)
        {
            if (manufFilter.isEmpty() == false && manuf.contains(manufFilter, Qt::CaseInsensitive) == false)
                continue;

            QJsonArray models;
            const QStringList modelList = m_doc->fixtureDefCache()->models(manuf);
            for (const QString &model : modelList)
            {
                if (modelFilter.isEmpty() == false && model.contains(modelFilter, Qt::CaseInsensitive) == false)
                    continue;

                QLCFixtureDef *def = m_doc->fixtureDefCache()->fixtureDef(manuf, model);
                if (def == nullptr)
                    continue;

                QJsonObject modelObj;
                modelObj["model"] = model;
                QJsonArray modes;
                const QList<QLCFixtureMode*> modeList = def->modes();
                for (QLCFixtureMode *mode : modeList)
                {
                    QJsonObject modeObj;
                    modeObj["name"] = mode->name();
                    modeObj["channels"] = int(mode->channels().count());
                    modes.append(modeObj);
                }
                modelObj["modes"] = modes;
                modelObj["type"] = QLCFixtureDef::typeToString(def->type());
                models.append(modelObj);
            }
            if (models.isEmpty())
                continue;

            QJsonObject manufObj;
            manufObj["manufacturer"] = manuf;
            manufObj["models"] = models;
            manufacturers.append(manufObj);
        }

        QJsonObject result;
        result["manufacturers"] = manufacturers;
        return agentOk(verb, reqId, result);
    }
    else if (verb == "getFunction")
    {
        Function *function = m_doc->function(jsonId(args, "id"));
        if (function == nullptr)
            return agentErr(verb, reqId, "no such function");

        QJsonObject fn;
        fn["id"] = int(function->id());
        fn["name"] = function->name();
        fn["type"] = function->typeString();
        fn["running"] = function->isRunning();
        fn["duration"] = int(function->totalDuration());
        fn["fadeIn"] = int(function->fadeInSpeed());
        fn["fadeOut"] = int(function->fadeOutSpeed());

        Scene *scene = qobject_cast<Scene*>(function);
        if (scene != nullptr)
        {
            QJsonArray values;
            const QList<SceneValue> sceneValues = scene->values();
            for (const SceneValue &scv : sceneValues)
            {
                QJsonObject v;
                v["fixture"] = int(scv.fxi);
                v["channel"] = int(scv.channel);
                v["value"] = int(scv.value);
                values.append(v);
            }
            fn["values"] = values;

            QJsonArray fixtureIds;
            for (quint32 fixtureId : scene->fixtures())
                fixtureIds.append(int(fixtureId));
            fn["fixtures"] = fixtureIds;
        }

        Chaser *chaser = qobject_cast<Chaser*>(function);
        if (chaser != nullptr)
        {
            QJsonArray steps;
            const QList<ChaserStep> chaserSteps = chaser->steps();
            for (const ChaserStep &step : chaserSteps)
            {
                QJsonObject s;
                s["function"] = int(step.fid);
                s["fadeIn"] = int(step.fadeIn);
                s["hold"] = int(step.hold);
                s["fadeOut"] = int(step.fadeOut);
                steps.append(s);
            }
            fn["steps"] = steps;
            fn["runOrder"] = int(chaser->runOrder());
        }

        Collection *collection = qobject_cast<Collection*>(function);
        if (collection != nullptr)
        {
            QJsonArray functionIds;
            for (quint32 fid : collection->functions())
                functionIds.append(int(fid));
            fn["functions"] = functionIds;
        }

        Sequence *sequence = qobject_cast<Sequence*>(function);
        if (sequence != nullptr)
            fn["boundScene"] = int(sequence->boundSceneID());

        return agentOk(verb, reqId, fn);
    }
    else if (verb == "getWidget")
    {
        if (m_vc == nullptr)
            return agentErr(verb, reqId, "virtual console not available");

        VCWidget *widget = m_vc->widget(jsonId(args, "id"));
        if (widget == nullptr)
            return agentErr(verb, reqId, "no such widget");

        QJsonObject result = widgetToJson(widget);

        /* Widgets serialize themselves to XML: the most complete and
         * always up-to-date view of one widget's configuration. */
        QByteArray buffer;
        QXmlStreamWriter writer(&buffer);
        widget->saveXML(&writer);
        result["xml"] = QString::fromUtf8(buffer);

        return agentOk(verb, reqId, result);
    }

    /************************************************************************
     * Fixtures
     ************************************************************************/

    else if (verb == "addFixture")
    {
        const QString manuf = jsonString(args, "manufacturer");
        const QString model = jsonString(args, "model");
        const QString modeName = jsonString(args, "mode");
        const QString name = jsonString(args, "name");
        int universe = args.value("universe").toInt(0);
        int address = args.value("address").toInt(0);
        int quantity = args.value("quantity").toInt(1);
        int gap = args.value("gap").toInt(0);

        if (manuf.isEmpty() || model.isEmpty())
            return agentErr(verb, reqId, "manufacturer and model are required");

        QLCFixtureDef *def = m_doc->fixtureDefCache()->fixtureDef(manuf, model);
        if (def == nullptr)
            return agentErr(verb, reqId, QString("unknown fixture definition: %1 %2").arg(manuf, model));

        QLCFixtureMode *mode = modeName.isEmpty() ? def->modes().first() : def->mode(modeName);
        if (mode == nullptr)
            return agentErr(verb, reqId, QString("unknown mode '%1' for %2 %3").arg(modeName, manuf, model));

        const int channels = mode->channels().count();
        if (channels == 0)
            return agentErr(verb, reqId, "fixture mode has no channels");

        QJsonArray created;
        for (int i = 0; i < quantity; i++)
        {
            if (address + channels > UNIVERSE_SIZE)
            {
                universe++;
                address = 0;
                if (m_doc->inputOutputMap()->getUniverseID(universe) == InputOutputMap::invalidUniverse())
                {
                    m_doc->inputOutputMap()->addUniverse();
                    m_doc->inputOutputMap()->startUniverses();
                }
            }

            Fixture *fixture = new Fixture(m_doc);
            fixture->setAddress(quint32(address));
            fixture->setUniverse(quint32(universe));
            fixture->setFixtureDefinition(def, mode);

            if (m_doc->addFixture(fixture) == false)
            {
                delete fixture;
                return agentErr(verb, reqId, "engine refused the fixture");
            }

            if (name.isEmpty())
                fixture->setName(QString("%1 %2 [%3]").arg(manuf, model).arg(fixture->id()));
            else
                fixture->setName(quantity > 1 ? QString("%1 %2").arg(name).arg(i + 1) : name);

            created.append(int(fixture->id()));
            address += channels + gap;
        }

        QJsonObject result;
        result["ids"] = created;
        return agentOk(verb, reqId, result);
    }
    else if (verb == "removeFixture")
    {
        const quint32 id = jsonId(args, "id");
        if (m_doc->fixture(id) == nullptr)
            return agentErr(verb, reqId, "no such fixture");
        m_doc->deleteFixture(id);
        QJsonObject result;
        result["removed"] = int(id);
        return agentOk(verb, reqId, result);
    }
    else if (verb == "setFixtureAddress")
    {
        Fixture *fixture = m_doc->fixture(jsonId(args, "id"));
        if (fixture == nullptr)
            return agentErr(verb, reqId, "no such fixture");

        if (args.contains("universe"))
            fixture->setUniverse(quint32(args.value("universe").toInt()));
        if (args.contains("address"))
            fixture->setAddress(quint32(args.value("address").toInt()));

        QJsonObject result;
        result["id"] = int(fixture->id());
        result["universe"] = int(fixture->universe());
        result["address"] = int(fixture->address());
        return agentOk(verb, reqId, result);
    }
    else if (verb == "setFixtureName")
    {
        Fixture *fixture = m_doc->fixture(jsonId(args, "id"));
        if (fixture == nullptr)
            return agentErr(verb, reqId, "no such fixture");
        fixture->setName(jsonString(args, "name"));
        QJsonObject result;
        result["id"] = int(fixture->id());
        result["name"] = fixture->name();
        return agentOk(verb, reqId, result);
    }

    /************************************************************************
     * Universes / patching
     ************************************************************************/

    else if (verb == "patchUniverse")
    {
        const int universe = args.value("universe").toInt(-1);
        const QString plugin = jsonString(args, "plugin");
        const QString direction = jsonString(args, "direction", "output").toLower();
        const quint32 line = quint32(args.value("line").toInt(0));

        if (universe < 0 || plugin.isEmpty())
            return agentErr(verb, reqId, "universe and plugin are required");

        bool ok = false;
        if (direction == "input")
            ok = m_doc->inputOutputMap()->setInputPatch(quint32(universe), plugin, "", "", line);
        else if (direction == "feedback")
            ok = m_doc->inputOutputMap()->setOutputPatch(quint32(universe), plugin, "", "", line, true);
        else
            ok = m_doc->inputOutputMap()->setOutputPatch(quint32(universe), plugin, "", "", line, false);

        if (ok == false)
            return agentErr(verb, reqId, QString("could not patch universe %1 to %2 line %3").arg(universe).arg(plugin).arg(line));

        QJsonObject result;
        result["universe"] = universe;
        result["plugin"] = plugin;
        result["line"] = int(line);
        result["direction"] = direction;
        return agentOk(verb, reqId, result);
    }

    /************************************************************************
     * Functions
     ************************************************************************/

    else if (verb == "createFunction")
    {
        const QString type = jsonString(args, "type");
        const QString name = jsonString(args, "name");
        QJsonArray fixtureIds = args.value("fixtureIds").toArray();

        Function *function = nullptr;
        if (type.compare("Scene", Qt::CaseInsensitive) == 0)
        {
            Scene *scene = new Scene(m_doc);
            for (const QJsonValue &value : fixtureIds)
            {
                Fixture *fixture = m_doc->fixture(quint32(value.toInt()));
                if (fixture != nullptr)
                    scene->addFixture(fixture->id());
            }
            function = scene;
        }
        else if (type.compare("Sequence", Qt::CaseInsensitive) == 0)
        {
            /* a Sequence depends on a hidden bound Scene, as upstream does */
            Scene *scene = new Scene(m_doc);
            scene->setVisible(false);
            if (m_doc->addFunction(scene) == false)
            {
                delete scene;
                return agentErr(verb, reqId, "could not create the bound scene");
            }
            for (const QJsonValue &value : fixtureIds)
            {
                Fixture *fixture = m_doc->fixture(quint32(value.toInt()));
                if (fixture != nullptr)
                    scene->addFixture(fixture->id());
            }
            Sequence *sequence = new Sequence(m_doc);
            sequence->setBoundSceneID(scene->id());
            function = sequence;
        }
        else if (type.compare("Chaser", Qt::CaseInsensitive) == 0)
        {
            Chaser *chaser = new Chaser(m_doc);
            chaser->setDuration(1000);
            function = chaser;
        }
        else if (type.compare("Collection", Qt::CaseInsensitive) == 0)
        {
            function = new Collection(m_doc);
        }
        else
        {
            return agentErr(verb, reqId, QString("unsupported function type '%1'").arg(type));
        }

        if (name.isEmpty() == false)
            function->setName(name);
        else
            function->setName(QString("%1 %2").arg(function->typeString()).arg(function->id()));

        if (m_doc->addFunction(function) == false)
        {
            delete function;
            return agentErr(verb, reqId, "engine refused the function");
        }

        QJsonObject result;
        result["id"] = int(function->id());
        result["name"] = function->name();
        result["type"] = function->typeString();
        return agentOk(verb, reqId, result);
    }
    else if (verb == "renameFunction")
    {
        Function *function = m_doc->function(jsonId(args, "id"));
        if (function == nullptr)
            return agentErr(verb, reqId, "no such function");
        function->setName(jsonString(args, "name"));
        QJsonObject result;
        result["id"] = int(function->id());
        result["name"] = function->name();
        return agentOk(verb, reqId, result);
    }
    else if (verb == "deleteFunction")
    {
        const quint32 id = jsonId(args, "id");
        if (m_doc->function(id) == nullptr)
            return agentErr(verb, reqId, "no such function");
        m_doc->deleteFunction(id);
        QJsonObject result;
        result["removed"] = int(id);
        return agentOk(verb, reqId, result);
    }
    else if (verb == "setSceneValues")
    {
        Scene *scene = qobject_cast<Scene*>(m_doc->function(jsonId(args, "id")));
        if (scene == nullptr)
            return agentErr(verb, reqId, "no such scene");

        const bool merge = args.value("merge").toBool(true);
        if (merge == false)
            scene->clear();

        QJsonArray values = args.value("values").toArray();
        int applied = 0;
        for (const QJsonValue &value : values)
        {
            QJsonObject entry = value.toObject();
            const quint32 fixtureId = jsonId(entry, "fixture");
            const quint32 channel = jsonId(entry, "channel");
            const int dmxValue = entry.value("value").toInt(0);
            if (m_doc->fixture(fixtureId) == nullptr)
                continue;
            scene->addFixture(fixtureId);
            scene->setValue(fixtureId, channel, uchar(qBound(0, dmxValue, 255)));
            applied++;
        }

        QJsonObject result;
        result["id"] = int(scene->id());
        result["applied"] = applied;
        result["values"] = int(scene->values().count());
        return agentOk(verb, reqId, result);
    }
    else if (verb == "addChaserSteps")
    {
        Chaser *chaser = qobject_cast<Chaser*>(m_doc->function(jsonId(args, "id")));
        if (chaser == nullptr)
            return agentErr(verb, reqId, "no such chaser");

        QJsonArray steps = args.value("steps").toArray();
        int added = 0;
        for (const QJsonValue &value : steps)
        {
            QJsonObject entry = value.toObject();
            const quint32 fid = jsonId(entry, "functionId");
            if (m_doc->function(fid) == nullptr)
                continue;
            ChaserStep step(fid);
            step.fadeIn = uint(entry.value("fadeIn").toInt(0));
            step.hold = uint(entry.value("hold").toInt(0));
            step.fadeOut = uint(entry.value("fadeOut").toInt(0));
            chaser->addStep(step);
            added++;
        }

        QJsonObject result;
        result["id"] = int(chaser->id());
        result["added"] = added;
        result["steps"] = int(chaser->steps().count());
        return agentOk(verb, reqId, result);
    }
    else if (verb == "setCollectionFunctions")
    {
        Collection *collection = qobject_cast<Collection*>(m_doc->function(jsonId(args, "id")));
        if (collection == nullptr)
            return agentErr(verb, reqId, "no such collection");

        const QList<quint32> current = collection->functions();
        for (quint32 fid : current)
            collection->removeFunction(fid);

        QJsonArray functionIds = args.value("functionIds").toArray();
        int added = 0;
        for (const QJsonValue &value : functionIds)
        {
            if (m_doc->function(quint32(value.toInt())) == nullptr)
                continue;
            collection->addFunction(quint32(value.toInt()));
            added++;
        }

        QJsonObject result;
        result["id"] = int(collection->id());
        result["added"] = added;
        return agentOk(verb, reqId, result);
    }
    else if (verb == "setFunctionStatus")
    {
        Function *function = m_doc->function(jsonId(args, "id"));
        if (function == nullptr)
            return agentErr(verb, reqId, "no such function");

        const bool run = args.value("run").toBool(true);
        if (run && function->isRunning() == false)
            function->start(m_doc->masterTimer(), FunctionParent::master());
        else if (run == false && function->isRunning())
            function->stop(FunctionParent::master());

        QJsonObject result;
        result["id"] = int(function->id());
        result["running"] = function->isRunning();
        return agentOk(verb, reqId, result);
    }

    /************************************************************************
     * Virtual Console
     ************************************************************************/

    else if (verb == "addPage")
    {
        if (m_vc == nullptr)
            return agentErr(verb, reqId, "virtual console not available");

        const int count = m_vc->pagesCount();
        int index = args.value("index").toInt(-1);
        if (index < 0 || index > count)
            index = count;

        m_vc->addPage(index);

        QJsonObject result;
        result["index"] = index;
        result["pages"] = m_vc->pagesCount();
        return agentOk(verb, reqId, result);
    }
    else if (verb == "deletePage")
    {
        if (m_vc == nullptr)
            return agentErr(verb, reqId, "virtual console not available");

        const int index = args.value("index").toInt(-1);
        if (index < 0 || index >= m_vc->pagesCount())
            return agentErr(verb, reqId, "page index out of range");
        if (m_vc->pagesCount() == 1)
            return agentErr(verb, reqId, "the last page cannot be removed");

        m_vc->deletePage(index);

        QJsonObject result;
        result["removed"] = index;
        result["pages"] = m_vc->pagesCount();
        return agentOk(verb, reqId, result);
    }
    else if (verb == "addWidget")
    {
        if (m_vc == nullptr)
            return agentErr(verb, reqId, "virtual console not available");

        const int pageIndex = args.value("page").toInt(0);
        const QString type = jsonString(args, "type");
        const QString element = widgetElement(type);
        if (element.isEmpty())
            return agentErr(verb, reqId, QString("unsupported widget type '%1'").arg(type));

        VCFrame *page = m_vc->page(pageIndex);
        if (page == nullptr)
            return agentErr(verb, reqId, "page index out of range");

        /* next free widget ID across all pages */
        quint32 newId = 0;
        QList<VCWidget*> existing;
        for (int i = 0; i < m_vc->pagesCount(); i++)
        {
            collectWidgets(m_vc->page(i), existing, true);
            for (VCWidget *widget : existing)
                newId = qMax(newId, widget->id() + 1);
            existing.clear();
        }

        const quint32 functionId = jsonId(args, "functionId");
        const QByteArray xml = widgetXml(element,
                                         newId,
                                         jsonString(args, "caption", type),
                                         args.value("x").toInt(0),
                                         args.value("y").toInt(0),
                                         args.value("w").toInt(120),
                                         args.value("h").toInt(60),
                                         functionId);

        QXmlStreamReader reader(xml);
        while (reader.atEnd() == false && reader.isStartElement() == false)
            reader.readNext();

        if (page->loadWidgetXML(reader, true) == false)
            return agentErr(verb, reqId, "could not create widget (invalid description)");

        VCWidget *widget = m_vc->widget(newId);
        if (widget == nullptr)
            return agentErr(verb, reqId, "widget was created but not registered");

        /* optional extra properties, applied by their QML property names */
        QJsonObject props = args.value("props").toObject();
        for (auto it = props.begin(); it != props.end(); ++it)
            widget->setProperty(it.key().toUtf8().constData(), agentVariant(it.key(), it.value()));

        QJsonObject result = widgetToJson(widget);
        result["created"] = true;
        return agentOk(verb, reqId, result);
    }
    else if (verb == "setWidget")
    {
        if (m_vc == nullptr)
            return agentErr(verb, reqId, "virtual console not available");

        VCWidget *widget = m_vc->widget(jsonId(args, "id"));
        if (widget == nullptr)
            return agentErr(verb, reqId, "no such widget");

        if (args.contains("caption"))
            widget->setCaption(jsonString(args, "caption"));

        if (args.contains("functionId"))
            widget->setProperty("functionID", jsonId(args, "functionId"));

        if (args.contains("x") || args.contains("y") || args.contains("w") || args.contains("h"))
        {
            QRectF geometry = widget->geometry();
            if (args.contains("x")) geometry.moveLeft(args.value("x").toDouble());
            if (args.contains("y")) geometry.moveTop(args.value("y").toDouble());
            if (args.contains("w")) geometry.setWidth(args.value("w").toDouble());
            if (args.contains("h")) geometry.setHeight(args.value("h").toDouble());
            widget->setGeometry(geometry);
        }

        QJsonObject props = args.value("props").toObject();
        for (auto it = props.begin(); it != props.end(); ++it)
            widget->setProperty(it.key().toUtf8().constData(), agentVariant(it.key(), it.value()));

        return agentOk(verb, reqId, widgetToJson(widget));
    }
    else if (verb == "removeWidget")
    {
        if (m_vc == nullptr)
            return agentErr(verb, reqId, "virtual console not available");

        const quint32 id = jsonId(args, "id");
        if (m_vc->widget(id) == nullptr)
            return agentErr(verb, reqId, "no such widget");

        m_vc->deleteVCWidgets(QVariantList() << id);

        QJsonObject result;
        result["removed"] = int(id);
        return agentOk(verb, reqId, result);
    }

    /************************************************************************
     * Project file
     ************************************************************************/

    else if (verb == "saveProject")
    {
        const QString fileName = jsonString(args, "path");
        if (fileName.isEmpty())
            return agentErr(verb, reqId, "path is required (the agent does not know the current project file name)");

        /* same procedure as App::saveXML: write a temp file, then rename */
        QString tempFileName(fileName);
        tempFileName += ".temp";

        QFile file(tempFileName);
        if (file.open(QIODevice::WriteOnly | QIODevice::Truncate) == false)
            return agentErr(verb, reqId, QString("cannot open %1 for writing").arg(tempFileName));

        QXmlStreamWriter doc(&file);
        doc.setAutoFormatting(true);
        doc.setAutoFormattingIndent(1);
        doc.writeStartDocument();
        doc.writeDTD(QString("<!DOCTYPE %1>").arg(KXMLQLCWorkspace));
        doc.writeStartElement(KXMLQLCWorkspace);
        doc.writeAttribute("xmlns", QString("%1%2").arg(KXMLQLCplusNamespace).arg(KXMLQLCWorkspace));
        doc.writeAttribute(KXMLQLCWorkspaceWindow, "FunctionManager");

        doc.writeStartElement(KXMLQLCCreator);
        doc.writeTextElement(KXMLQLCCreatorName, APPNAME);
        doc.writeTextElement(KXMLQLCCreatorVersion, APPVERSION);
        doc.writeTextElement(KXMLQLCCreatorAuthor, "QLC+ Agent API");
        doc.writeEndElement();

        m_doc->saveXML(&doc);
        if (m_vc != nullptr)
            m_vc->saveXML(&doc);

        doc.writeEndElement();
        doc.writeEndDocument();

        if (doc.hasError())
        {
            file.close();
            file.remove();
            return agentErr(verb, reqId, "error while writing the project XML");
        }

        file.close();
        if (file.error() != QFile::NoError)
            return agentErr(verb, reqId, QString("write error: %1").arg(int(file.error())));

        QFile target(fileName);
        if (target.exists() && target.remove() == false)
            return agentErr(verb, reqId, QString("cannot replace %1").arg(fileName));
        if (file.rename(fileName) == false)
            return agentErr(verb, reqId, QString("cannot rename the temp file to %1").arg(fileName));

        m_doc->resetModified();

        QJsonObject result;
        result["saved"] = fileName;
        return agentOk(verb, reqId, result);
    }

    return agentErr(verb, reqId, QString("unknown verb '%1'").arg(verb));
}
