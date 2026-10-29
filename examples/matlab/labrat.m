classdef labrat < handle
    % LABRAT client for MATLAB. Sends LABRAT messages to a nest or rat over HTTP.
    %
    %   lab = labrat("http://nest.local:8780");          % token from LABRAT_TOKEN
    %   lab = labrat("http://nest.local:8780", token);
    %   lab.devices()
    %   lab.set("bench-rat/psu", "VoltageLevel", 3.3, "1");
    %   v = lab.get("bench-rat/psu", "VoltageLevel", "1");
    %   r = lab.call("bench-rat/psu", "Measure", struct("type", "current"), "1");
    %   lab.stop("finished");
    %
    % A refused request raises an error with the identifier labrat:<code>, for example labrat:limit.

    properties
        Url string
        Token string
        Timeout double = 30
    end

    methods
        function obj = labrat(url, token)
            if nargin < 1 || strlength(string(url)) == 0, url = getenv("LABRAT_URL"); end
            if nargin < 2, token = getenv("LABRAT_TOKEN"); end
            obj.Url = strip(string(url), "right", "/");
            obj.Token = string(token);
        end

        function value = send(obj, msg)
            % Send one message (a struct) and return its value.
            headers = [matlab.net.http.field.ContentTypeField("application/json"), ...
                       matlab.net.http.HeaderField("Authorization", "Bearer " + obj.Token)];
            req = matlab.net.http.RequestMessage("POST", headers, matlab.net.http.MessageBody(jsonencode(msg)));
            opts = matlab.net.http.HTTPOptions("ConnectTimeout", obj.Timeout, "ResponseTimeout", obj.Timeout);
            resp = req.send(obj.Url + "/api/v1/message", opts);
            r = resp.Body.Data;
            if ischar(r) || isstring(r), r = jsondecode(char(r)); end
            if ~r.ok
                error("labrat:" + r.error.code, "%s", r.error.message);
            end
            if isfield(r, "value"), value = r.value; else, value = []; end
        end

        function d = devices(obj)
            d = obj.send(struct("op", "list"));
        end

        function d = describe(obj, device)
            d = obj.send(struct("op", "describe", "device", device));
        end

        function v = get(obj, device, name, channel)
            m = struct("op", "get", "device", device, "name", name);
            if nargin > 3, m.channel = string(channel); end
            v = obj.send(m);
        end

        function v = set(obj, device, name, value, channel)
            m = struct("op", "set", "device", device, "name", name, "value", value);
            if nargin > 4, m.channel = string(channel); end
            v = obj.send(m);
        end

        function v = call(obj, device, name, args, channel)
            m = struct("op", "call", "device", device, "name", name);
            if nargin > 3 && ~isempty(args), m.args = args; end
            if nargin > 4, m.channel = string(channel); end
            v = obj.send(m);
        end

        function v = lock(obj, device, minutes)
            m = struct("op", "lock", "device", device);
            if nargin > 2, m.args = struct("minutes", minutes); end
            v = obj.send(m);
        end

        function v = unlock(obj, device)
            v = obj.send(struct("op", "unlock", "device", device));
        end

        function v = stop(obj, reason)
            if nargin < 2, reason = "MATLAB"; end
            v = obj.send(struct("op", "stop", "args", struct("reason", reason)));
        end
    end
end
